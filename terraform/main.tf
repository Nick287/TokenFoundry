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
  cosmos_account_name            = module.cosmos.account_name
  cosmos_account_id              = module.cosmos.account_id
}

# --- Model backends: pool + circuit breaker (preview API version, via azapi) ---
module "apim_backends" {
  source = "./modules/apim-backends"

  apim_id      = module.apim.apim_id
  backend_urls = var.backend_urls
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
  image_tag                  = var.image_tag
  key_vault_uri              = module.keyvault.vault_uri
  vault_id                   = module.keyvault.vault_id
  cosmos_endpoint            = module.cosmos.endpoint
  cosmos_account_name        = module.cosmos.account_name
  cosmos_account_id          = module.cosmos.account_id
  app_insights_id            = module.monitor.app_insights_id
  apim_service_name          = module.apim.apim_name
  apim_id                    = module.apim.apim_id
  acr_id                     = module.acr.registry_id
  acr_login_server           = module.acr.login_server
  database_url_secret_uri    = module.appsecrets.database_url_secret_uri
  jwt_secret_uri             = module.appsecrets.jwt_secret_uri
  admin_password_secret_uri  = module.appsecrets.admin_password_secret_uri
  admin_username             = "admin"

  # 方案 A: control plane reads hub outputs from remote state (no terraform here).
  tfstate_storage_account    = module.deployer.tfstate_storage_account_name
  tfstate_storage_account_id = module.deployer.tfstate_storage_account_id
  tfstate_container          = module.deployer.tfstate_container_name
}
