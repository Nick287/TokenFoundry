locals {
  # Keep a single replica: the hub's in-memory session table (_SESSIONS) and
  # login-attempt tracker (_LOGIN_ATTEMPTS) are per-process, and APIM session
  # affinity assumes a stable backend. (The hub is otherwise stateless — SQLite
  # is an ephemeral scratch DB under /tmp, no shared storage.)
  replicas = 1

  # Build context is the repo root (one level up from this module).
  build_context = abspath("${path.module}/..")

  # Stable hash of everything baked into the image: the hub/ package plus the
  # Dockerfile and requirements.txt. __pycache__/*.pyc are excluded so stray
  # bytecode never perturbs the hash (keeps apply idempotent).
  src_files = sort([
    for f in setunion(fileset(local.build_context, "hub/**"),
    toset(["Dockerfile", "requirements.txt"])) :
    f if !can(regex("__pycache__|\\.py[cod]$", f))
  ])
  src_hash = sha1(join("\n",
  [for f in local.src_files : "${f}=${filesha1("${local.build_context}/${f}")}"]))

  # Image ref whose tag changes only when the source changes — so a code edit
  # produces a new image string and the Container App rolls a fresh revision.
  # When image_ref_override is set (P2: pre-built shared image), use it verbatim
  # and skip the per-account az acr build entirely (see terraform_data.build).
  image_ref = var.image_ref_override != "" ? var.image_ref_override : "${var.image_name}:${substr(local.src_hash, 0, 12)}"
}

resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location

  # SecurityControl=Ignore MUST be present the instant the RG is created, so the
  # org security policy doesn't block resources (e.g. the storage account) made
  # inside it. In Terraform, tags are part of the RG's create API call — they're
  # applied atomically with the RG, before anything that depends on it. Every
  # resource below references azurerm_resource_group.rg, so they are all created
  # strictly after this tag exists.
  tags = {
    SecurityControl = "Ignore"
  }
}

# --- Container registry (create if missing, else use existing) ------------
#
# Terraform can't probe for existence at plan time, so `create_acr` selects the
# mode: true (default) -> create a new registry in the app's resource group
# with a globally-unique name (prefix + random suffix); false -> look up an
# existing registry named acr_name in acr_resource_group_name. Either way the
# rest of the config reads through local.acr_* so nothing else changes.

resource "random_string" "acr_suffix" {
  count   = var.create_acr ? 1 : 0
  length  = 8
  upper   = false
  special = false
}

resource "azurerm_container_registry" "acr" {
  count               = var.create_acr ? 1 : 0
  name                = "${var.prefix}${random_string.acr_suffix[0].result}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic" # supports ACR Tasks (cloud build) + MI pull
  admin_enabled       = false
}

data "azurerm_container_registry" "acr" {
  count               = var.create_acr ? 0 : 1
  name                = var.acr_name
  resource_group_name = var.acr_resource_group_name
}

locals {
  acr_id           = var.create_acr ? azurerm_container_registry.acr[0].id : data.azurerm_container_registry.acr[0].id
  acr_name         = var.create_acr ? azurerm_container_registry.acr[0].name : data.azurerm_container_registry.acr[0].name
  acr_login_server = var.create_acr ? azurerm_container_registry.acr[0].login_server : data.azurerm_container_registry.acr[0].login_server
}

# Cloud build via ACR Tasks — uploads the build context and builds/pushes the
# image without a local docker daemon. Re-runs only when src_hash changes.
#
# The context is staged into a clean temp dir holding only what the image needs
# (Dockerfile, requirements.txt, hub/). Packing the repo root directly is
# fragile: `az acr build` ignores .dockerignore here and would try to tar large
# / locked files (e.g. infra/.terraform, the live tfstate) and fail with EACCES.
resource "terraform_data" "build" {
  # P2: when a pre-built shared image is referenced (image_ref_override set), the
  # per-account deploy must NOT build — count=0 disables the az acr build.
  count            = var.image_ref_override == "" ? 1 : 0
  triggers_replace = local.src_hash

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -eu
      ctx="${local.build_context}"
      staging="$(mktemp -d)"
      trap 'rm -rf "$staging"' EXIT
      cp "$ctx/Dockerfile" "$ctx/requirements.txt" "$staging/"
      cp -r "$ctx/hub" "$staging/hub"
      find "$staging/hub" -name __pycache__ -type d -prune -exec rm -rf {} +
      az acr build --registry ${local.acr_name} --image ${local.image_ref} "$staging"
    EOT
  }
}

# --- Identity used by the Container App to pull from ACR ------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.prefix}-id"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = local.acr_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- Container Apps environment + app -------------------------------------
#
# The hub is stateless: no Azure Files storage account / share / volume. All
# durable secrets are injected as Container App secret-env at deploy time, and
# SQLite is an ephemeral scratch DB under /tmp (recreated empty on cold start).

resource "azurerm_container_app_environment" "env" {
  name                = "${var.prefix}-env"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
}



resource "azurerm_container_app" "hub" {
  name                         = "${var.prefix}-hub"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = local.acr_login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  # Copilot OAuth token as a Container App secret (only when provided). Injected
  # into the container as COPILOT_OAUTH_TOKEN so the hub is authenticated for its
  # GitHub account without going through the portal device flow.
  dynamic "secret" {
    for_each = var.copilot_oauth_token != "" ? toset([1]) : toset([])
    content {
      name  = "copilot-oauth-token"
      value = var.copilot_oauth_token
    }
  }

  # Deploy-time admin token as a Container App secret (only when provided).
  # Injected as HUB_ADMIN_TOKEN so the control plane can call the management API
  # (POST /api/keys to mint a hub key) without the portal login flow.
  dynamic "secret" {
    for_each = var.hub_admin_token != "" ? toset([1]) : toset([])
    content {
      name  = "hub-admin-token"
      value = var.hub_admin_token
    }
  }

  # Deploy-time hub /v1 API key as a Container App secret (only when provided).
  # Injected as HUB_API_KEY — the durable inbound credential APIM authenticates
  # with (the hub is stateless, so portal-created SQLite keys don't persist).
  dynamic "secret" {
    for_each = var.hub_api_key != "" ? toset([1]) : toset([])
    content {
      name  = "hub-api-key"
      value = var.hub_api_key
    }
  }

  ingress {
    external_enabled = true
    target_port      = var.container_port
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = local.replicas
    max_replicas = local.replicas

    container {
      name   = "${var.prefix}-hub"
      image  = "${local.acr_login_server}/${local.image_ref}"
      cpu    = var.cpu
      memory = var.memory

      # Ephemeral scratch dir for the SQLite DB — no persistent mount. The hub
      # is stateless; nothing here is expected to survive a restart.
      env {
        name  = "HUB_DATA_DIR"
        value = "/tmp/hubdata"
      }

      env {
        name  = "HUB_REQUIRE_AUTH"
        value = tostring(var.require_auth)
      }

      env {
        name  = "HUB_LOGIN_MAX_FAILS"
        value = tostring(var.login_max_fails)
      }

      env {
        name  = "HUB_LOGIN_LOCK_SECONDS"
        value = tostring(var.login_lock_seconds)
      }

      # COPILOT_OAUTH_TOKEN from the Container App secret (only when provided).
      # Authenticates the hub for its GitHub account without the portal flow.
      dynamic "env" {
        for_each = var.copilot_oauth_token != "" ? toset([1]) : toset([])
        content {
          name        = "COPILOT_OAUTH_TOKEN"
          secret_name = "copilot-oauth-token"
        }
      }

      # HUB_ADMIN_TOKEN from the Container App secret (only when provided). Lets
      # the control plane call the management API to mint a hub key post-deploy.
      dynamic "env" {
        for_each = var.hub_admin_token != "" ? toset([1]) : toset([])
        content {
          name        = "HUB_ADMIN_TOKEN"
          secret_name = "hub-admin-token"
        }
      }

      # HUB_API_KEY from the Container App secret (only when provided). The
      # durable inbound /v1/* credential the control plane / APIM authenticate
      # with (portal-created SQLite keys don't persist on a stateless hub).
      dynamic "env" {
        for_each = var.hub_api_key != "" ? toset([1]) : toset([])
        content {
          name        = "HUB_API_KEY"
          secret_name = "hub-api-key"
        }
      }
    }
  }

  # terraform_data.build is count-gated (0 when using a pre-built image), so use
  # a splat reference — [] when skipped, [<build>] when building. Either way the
  # app waits for the build only if one actually runs.
  depends_on = [azurerm_role_assignment.acr_pull, terraform_data.build]
}
