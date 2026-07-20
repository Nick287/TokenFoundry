# Container Apps environment + ONE app (aca-app): a single image serving both
# the FastAPI API and the built React portal. Its system-assigned identity is
# what the control plane uses for Key Vault / Cosmos / APIM management
# (DefaultAzureCredential). A dedicated user-assigned identity pulls the image.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }
variable "subscription_id" { type = string }

# NOTE vs Bicep: azurerm's Container App Environment takes only the workspace id
# and resolves the shared key itself — no customerId + sharedKey plumbing.
variable "log_analytics_workspace_id" { type = string }

# Workspace customerId (GUID) — the control plane queries the dedicated
# ApiManagementGatewayLlmLog table via query_workspace(customerId) for token
# metering. Distinct from log_analytics_workspace_id above (that's the ARM
# resource id, consumed by the CAE).
variable "log_analytics_customer_id" { type = string }

variable "image_tag" { type = string }
variable "key_vault_uri" { type = string }
variable "vault_id" { type = string }
variable "cosmos_endpoint" { type = string }
variable "cosmos_account_name" { type = string }
variable "cosmos_account_id" { type = string }
variable "app_insights_id" { type = string }
variable "apim_service_name" { type = string }
variable "apim_id" { type = string }
variable "acr_id" { type = string }
variable "acr_login_server" { type = string }
variable "acr_name" { type = string }
variable "keyvault_name" { type = string }
variable "database_url_secret_uri" { type = string }
variable "jwt_secret_uri" { type = string }
variable "admin_password_secret_uri" { type = string }
variable "admin_username" {
  type    = string
  default = "admin"
}

# --- 方案 A: remote-state wiring (control plane reads hub outputs from state) ---
variable "tfstate_storage_account" {
  type        = string
  description = "Storage account holding per-account terraform remote state."
}
variable "tfstate_container" {
  type        = string
  description = "Blob container for terraform remote state."
}
variable "tfstate_storage_account_id" {
  type        = string
  description = "Storage account id — scope for the control plane's Storage Blob Data Reader grant (reads per-account hub outputs from state)."
}
variable "github_repo_owner" {
  type        = string
  default     = "Nick287"
  description = "Owner of the repo hosting deploy-hub.yml (injected as TF_GITHUB_REPO_OWNER)."
}
variable "github_repo_name" {
  type        = string
  default     = "TokenFoundry"
  description = "Repo hosting deploy-hub.yml (injected as TF_GITHUB_REPO_NAME)."
}
variable "github_ref" {
  type        = string
  default     = "master"
  description = "Git ref the workflow_dispatch targets (injected as TF_GITHUB_REF)."
}

resource "azurerm_container_app_environment" "env" {
  name                       = "${var.name_prefix}-cae"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tags                       = var.tags
  log_analytics_workspace_id = var.log_analytics_workspace_id
}

# --- User-assigned identity dedicated to pulling from ACR ---
# A UAMI's principalId is known at create time, so AcrPull can be granted BEFORE
# the app exists — avoiding the system-identity chicken-and-egg where the app
# tries to pull before its own identity has been granted the role.
resource "azurerm_user_assigned_identity" "pull" {
  name                = "${var.name_prefix}-acrpull-id"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# Grant the pull identity AcrPull on the registry, before the app is created.
resource "azurerm_role_assignment" "acr_pull" {
  scope                            = var.acr_id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_user_assigned_identity.pull.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# Grant the pull identity Key Vault Secrets User so the platform can resolve the
# secret references below at startup. Pre-granted (like AcrPull) so it's in place
# before the app's first revision activates.
resource "azurerm_role_assignment" "kv_secrets_user" {
  scope                            = var.vault_id
  role_definition_name             = "Key Vault Secrets User"
  principal_id                     = azurerm_user_assigned_identity.pull.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

# --- Single app: API + portal in one image ---
resource "azurerm_container_app" "app" {
  name                         = substr("${var.name_prefix}-aca-${var.suffix}", 0, 32)
  resource_group_name          = var.resource_group_name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  tags                         = var.tags

  # SystemAssigned: runtime access to Key Vault / Cosmos / APIM
  # (DefaultAzureCredential). UserAssigned: pulls the image from the private ACR
  # (role pre-granted above).
  identity {
    type         = "SystemAssigned, UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.pull.id]
  }

  # Pull from the private ACR using the dedicated user-assigned identity.
  registry {
    server   = var.acr_login_server
    identity = azurerm_user_assigned_identity.pull.id
  }

  # Key Vault secret references resolved via the pull identity.
  secret {
    name                = "tf-database-url"
    key_vault_secret_id = var.database_url_secret_uri
    identity            = azurerm_user_assigned_identity.pull.id
  }
  secret {
    name                = "tf-jwt-secret"
    key_vault_secret_id = var.jwt_secret_uri
    identity            = azurerm_user_assigned_identity.pull.id
  }
  secret {
    name                = "tf-admin-password"
    key_vault_secret_id = var.admin_password_secret_uri
    identity            = azurerm_user_assigned_identity.pull.id
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 1
    max_replicas = 3

    container {
      name = "app"
      # Image ref assembled from the ACR login server + tag, so Terraform knows
      # the full name at apply time without the script passing it in. The deploy
      # script builds & pushes tokenfoundry:<image_tag> to this same ACR.
      image  = "${var.acr_login_server}/tokenfoundry:${var.image_tag}"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "TF_KEYVAULT_URI"
        value = var.key_vault_uri
      }
      env {
        name  = "TF_COSMOS_ENDPOINT"
        value = var.cosmos_endpoint
      }
      env {
        name  = "TF_APIM_SERVICE_NAME"
        value = var.apim_service_name
      }
      env {
        name  = "TF_APP_INSIGHTS_RESOURCE_ID"
        value = var.app_insights_id
      }
      env {
        name  = "TF_LOG_ANALYTICS_WORKSPACE_ID"
        value = var.log_analytics_customer_id
      }
      env {
        name  = "TF_RESOURCE_GROUP"
        value = var.resource_group_name
      }
      env {
        name  = "TF_AZURE_SUBSCRIPTION_ID"
        value = var.subscription_id
      }
      env {
        name  = "TF_ENVIRONMENT"
        value = "prod"
      }
      # Self-hosted login + DB connection (secrets from Key Vault).
      env {
        name        = "TF_DATABASE_URL"
        secret_name = "tf-database-url"
      }
      env {
        name        = "TF_JWT_SECRET"
        secret_name = "tf-jwt-secret"
      }
      env {
        name        = "TF_ADMIN_PASSWORD"
        secret_name = "tf-admin-password"
      }
      env {
        name  = "TF_ADMIN_USERNAME"
        value = var.admin_username
      }

      # 方案 A: the control plane triggers a GitHub Action (workflow_dispatch) that
      # runs the hub terraform, then reads the resulting outputs from remote state.
      # It does NOT run terraform itself. These point it at the state blob and the
      # workflow to dispatch. (The GitHub token is read from Key Vault at runtime,
      # not injected here.)
      env {
        name  = "TF_TFSTATE_STORAGE_ACCOUNT"
        value = var.tfstate_storage_account
      }
      env {
        name  = "TF_TFSTATE_CONTAINER"
        value = var.tfstate_container
      }
      env {
        name  = "TF_GITHUB_REPO_OWNER"
        value = var.github_repo_owner
      }
      env {
        name  = "TF_GITHUB_REPO_NAME"
        value = var.github_repo_name
      }
      env {
        name  = "TF_GITHUB_REF"
        value = var.github_ref
      }
      # Repo-variable sources for the Portal's "push SP creds to GitHub" flow
      # (app/api/deploy_config.py): the control plane sets GitHub Actions
      # variables HUB_ACR_NAME / HUB_LOCATION / HUB_KEYVAULT_NAME straight from
      # these — terraform hands over the bare names/region so the app does zero
      # string parsing (no runtime az query / terraform state).
      env {
        name  = "TF_ACR_LOGIN_SERVER"
        value = var.acr_login_server
      }
      env {
        name  = "TF_ACR_NAME"
        value = var.acr_name
      }
      env {
        name  = "TF_AZURE_LOCATION"
        value = var.location
      }
      env {
        name  = "TF_KEYVAULT_NAME"
        value = var.keyvault_name
      }
      env {
        # Image TAG the hub deploy references. deploy.sh builds gitmodel:<tag>
        # with this SAME tag (never "latest"), so the Portal's deploy-config
        # flow must publish gitmodel:<this-tag> as HUB_IMAGE_REF — not a
        # hard-coded :latest that doesn't exist in ACR.
        name  = "TF_HUB_IMAGE_TAG"
        value = var.image_tag
      }

      liveness_probe {
        transport        = "HTTP"
        path             = "/healthz"
        port             = 8000
        initial_delay    = 10
        interval_seconds = 30
      }
    }
  }

  # Ensure the pull identity holds AcrPull + KV Secrets User before the first
  # revision activates (mirrors the Bicep dependsOn race fix).
  depends_on = [
    azurerm_role_assignment.acr_pull,
    azurerm_role_assignment.kv_secrets_user,
  ]
}

# --- Post-app role assignments on the app's SYSTEM identity ---

# Manage APIM (create subscriptions / products / backends at runtime via
# DefaultAzureCredential). Needed only while the app runs, so the system
# identity (which exists only after the app is created) is fine — no startup race.
resource "azurerm_role_assignment" "apim_contributor" {
  scope                = var.apim_id
  role_definition_name = "API Management Service Contributor"
  principal_id         = azurerm_container_app.app.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# Read/WRITE on Key Vault. The control plane WRITES subscription keys + BYO
# secrets at runtime, so it needs Secrets Officer, not the read-only Secrets
# User the pull identity has for resolving secret refs.
resource "azurerm_role_assignment" "kv_secrets_officer" {
  scope                = var.vault_id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = azurerm_container_app.app.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# Monitoring Reader on App Insights so the usage-telemetry endpoint (KQL via
# azure-monitor-query) can read the requests table.
resource "azurerm_role_assignment" "monitoring_reader" {
  scope                = var.app_insights_id
  role_definition_name = "Monitoring Reader"
  principal_id         = azurerm_container_app.app.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# Log Analytics Reader on the WORKSPACE. Monitoring Reader above is scoped to the
# App Insights component, which lets query_resource(app_insights_id) read the
# requests/customMetrics tables — but the token-metering breakdown queries the
# dedicated ApiManagementGatewayLlmLog table via query_workspace(customerId),
# which runs at WORKSPACE scope and the component-scoped role does NOT cover.
# Without this the breakdown queries 403 and the Token 细分 groups render empty.
resource "azurerm_role_assignment" "app_law_reader" {
  scope                = var.log_analytics_workspace_id
  role_definition_name = "Log Analytics Reader"
  principal_id         = azurerm_container_app.app.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# Cosmos DB data-plane access. The account sets local_authentication_enabled
# to false (no keys), so the usage read/write path (DefaultAzureCredential =
# system identity) needs a Cosmos *data-plane* RBAC assignment — distinct from
# Azure control-plane RBAC. Built-in "Cosmos DB Data Contributor" (...0002)
# covers readMetadata + read + upsert.
resource "azurerm_cosmosdb_sql_role_assignment" "app_data_contributor" {
  resource_group_name = var.resource_group_name
  account_name        = var.cosmos_account_name
  role_definition_id  = "${var.cosmos_account_id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_container_app.app.identity[0].principal_id
  scope               = var.cosmos_account_id
}

# --- 方案 A: control plane reads per-account hub outputs from remote state ---
#
# The hub terraform runs in a GitHub Action (SP auth) and writes state to the
# tfstate storage. The control plane downloads hubs/<account_id>.tfstate to read
# the app_url / resource_group outputs — so its system identity needs read on the
# state blobs. Storage Blob Data READER (not Contributor): the control plane only
# reads state, never writes it. This is the ONLY new privilege 方案 A adds — far
# smaller than P2's subscription-Contributor deployer identity, which is gone.
resource "azurerm_role_assignment" "app_tfstate_reader" {
  scope                = var.tfstate_storage_account_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_container_app.app.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}
