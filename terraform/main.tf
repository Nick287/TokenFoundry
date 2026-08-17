# Token Foundry — root orchestrator (mirrors infra/main.bicep).
#
# Deploy:  terraform apply
# Preview: terraform plan      (the `az deployment group what-if` analogue)
#
# Secrets are read from TF_VAR_pg_admin_password / TF_VAR_jwt_secret /
# TF_VAR_admin_password — export them before running plan/apply.

data "azurerm_client_config" "current" {}

# Terraform owns the resource group (Bicep assumed it pre-existed).
resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}

locals {
  tags = {
    project         = "token-foundry"
    environment     = var.environment_name
    SecurityControl = "Ignore"
  }

  # uniqueString(resourceGroup().id) equivalent: a deterministic hex suffix
  # derived from the RG id. Hex satisfies the strict alphanumeric-only charset
  # of Key Vault / ACR / Cosmos names. Per-resource length caps are applied in
  # each module via substr() to honour the Bicep take(..., N) limits.
  suffix = substr(md5(azurerm_resource_group.this.id), 0, 13)
}

# --- Observability foundation (Log Analytics + App Insights) ---
module "monitor" {
  source      = "./modules/monitor"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  sampling_percentage = var.app_insights_sampling_percentage
}

# --- Secrets (Key Vault) ---
module "keyvault" {
  source      = "./modules/keyvault"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  suffix              = local.suffix
  tenant_id           = data.azurerm_client_config.current.tenant_id
  deployer_object_id  = data.azurerm_client_config.current.object_id
}

# --- Metadata DB (PostgreSQL Flexible Server) ---
module "postgres" {
  source      = "./modules/postgres"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  suffix              = local.suffix
  admin_login         = var.pg_admin_login
  admin_password      = var.pg_admin_password
}

# --- Usage store (Cosmos DB for NoSQL) ---
module "cosmos" {
  source      = "./modules/cosmos"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  suffix              = local.suffix
  throughput_rus      = var.cosmos_throughput_rus
}

# --- Usage transport (Event Hub + Capture to Blob) ---
# Every GitModel hub emits one event per completed request; Capture drains them
# to Avro blobs, and the control plane's import job turns those into Cosmos
# documents. See modules/eventhub/main.tf for why it is a bus and not a direct
# write.
module "eventhub" {
  source      = "./modules/eventhub"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  suffix              = local.suffix
}

# --- Audit archive (raw request/response bodies, opt-in per tenant) ---
# Its own storage account rather than a container in the Capture one: this holds
# customer content, not billing telemetry, and must carry its own retention and
# its own (much shorter) access list. See modules/audit/main.tf.
module "audit" {
  source      = "./modules/audit"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  suffix              = local.suffix
}

# --- Container Registry (holds the single API+portal image) ---
module "acr" {
  source      = "./modules/acr"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  suffix              = local.suffix
}

# --- AI gateway (APIM) ---
module "apim" {
  source      = "./modules/apim"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name            = azurerm_resource_group.this.name
  suffix                         = local.suffix
  publisher_email                = var.publisher_email
  publisher_name                 = var.publisher_name
  app_insights_id                = module.monitor.app_insights_id
  app_insights_connection_string = module.monitor.app_insights_connection_string
  sku_name                       = var.apim_sku
  log_analytics_workspace_id     = module.monitor.log_analytics_id
  sampling_percentage            = var.apim_sampling_percentage
}

# --- App secrets in Key Vault (DB connection string, JWT secret, admin pwd) ---
module "appsecrets" {
  source = "./modules/appsecrets"

  vault_id       = module.keyvault.vault_id
  pg_login       = var.pg_admin_login
  pg_fqdn        = module.postgres.server_fqdn
  pg_password    = var.pg_admin_password
  jwt_secret     = var.jwt_secret
  admin_password = var.admin_password
  # Gate: don't write secrets until the deployer's Secrets Officer role is
  # granted and RBAC has propagated (keyvault module's time_sleep). Prevents 403.
  secrets_ready = module.keyvault.secrets_ready
}

# --- 方案 A: remote-state storage for per-account hub deploys ---
# The hub terraform runs in a GitHub Action (SP auth), not here — this module now
# provides ONLY the shared blob storage for per-account remote state. The control
# plane reads outputs from it (Storage Blob Data Reader granted in containerapps).
module "deployer" {
  source      = "./modules/deployer"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name = azurerm_resource_group.this.name
  suffix              = local.suffix
}

# --- Container App: single app (API + portal in one image) ---
module "containerapps" {
  source      = "./modules/containerapps"
  name_prefix = var.name_prefix
  location    = var.location
  tags        = local.tags

  resource_group_name        = azurerm_resource_group.this.name
  suffix                     = local.suffix
  subscription_id            = data.azurerm_client_config.current.subscription_id
  log_analytics_workspace_id = module.monitor.log_analytics_id
  log_analytics_customer_id  = module.monitor.log_analytics_customer_id
  image_tag                  = var.image_tag
  hub_image_tag              = var.hub_image_tag
  key_vault_uri              = module.keyvault.vault_uri
  keyvault_name              = module.keyvault.vault_name
  vault_id                   = module.keyvault.vault_id
  cosmos_endpoint            = module.cosmos.endpoint
  cosmos_account_name        = module.cosmos.account_name
  cosmos_account_id          = module.cosmos.account_id
  app_insights_id            = module.monitor.app_insights_id
  apim_service_name          = module.apim.apim_name
  apim_id                    = module.apim.apim_id
  acr_id                     = module.acr.registry_id
  acr_login_server           = module.acr.login_server
  acr_name                   = module.acr.registry_name
  database_url_secret_uri    = module.appsecrets.database_url_secret_uri
  jwt_secret_uri             = module.appsecrets.jwt_secret_uri
  admin_password_secret_uri  = module.appsecrets.admin_password_secret_uri
  admin_username             = "admin"
  github_repo_owner          = var.github_repo_owner
  github_repo_name           = var.github_repo_name

  # 方案 A: control plane reads hub outputs from remote state (no terraform here).
  tfstate_storage_account    = module.deployer.tfstate_storage_account_name
  tfstate_storage_account_id = module.deployer.tfstate_storage_account_id
  tfstate_container          = module.deployer.tfstate_container_name

  # Usage pipeline: republish the Event Hub coordinates to the hub deploy, and
  # read Capture's blobs for the import job.
  eventhub_namespace_id            = module.eventhub.namespace_id
  eventhub_fqdn                    = module.eventhub.fqdn
  eventhub_name                    = module.eventhub.eventhub_name
  usage_capture_storage_account    = module.eventhub.capture_storage_account_name
  usage_capture_storage_account_id = module.eventhub.capture_storage_account_id
  usage_capture_container          = module.eventhub.capture_container_name
  usage_capture_interval_seconds   = tostring(module.eventhub.capture_interval_seconds)

  # Audit archive: pure pass-through to the hub deploy. The control plane gets
  # no role on this account — it republishes the coordinates and records blob
  # paths, but reading a payload takes a separately-granted human role.
  audit_account_url     = module.audit.account_url
  audit_container       = module.audit.container_name
  audit_container_scope = module.audit.container_scope
  audit_retention_days  = tostring(module.audit.retention_days)
}
