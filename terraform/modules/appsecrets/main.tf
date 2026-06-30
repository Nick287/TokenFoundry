# App secrets — the runtime secrets the control plane reads from Key Vault:
# the PostgreSQL connection string (assembled here so the password never lands
# in app settings), the JWT signing secret, and the seed admin password.
#
# The Container App's pull identity is granted Key Vault Secrets User (in the
# containerapps module) and resolves these via secret references at startup.
#
# The deployer's Key Vault Secrets Officer role (needed to WRITE these secrets)
# is granted by the keyvault module; the secrets_ready gate below ensures that
# role is in place and RBAC has propagated before these writes run (no 403).

variable "vault_id" { type = string }
variable "pg_login" { type = string }
variable "pg_fqdn" { type = string }
variable "pg_password" {
  type      = string
  sensitive = true
}
variable "jwt_secret" {
  type      = string
  sensitive = true
}
variable "admin_password" {
  type      = string
  sensitive = true
}
variable "secrets_ready" {
  type        = string
  description = "Gate from the keyvault module — ensures the deployer's Secrets Officer role is granted and propagated before writing secrets."
}

# Anchor the secrets_ready gate as a dependable resource. Every secret below
# depends on this, so none is written until the keyvault module's role grant +
# RBAC propagation (time_sleep) has completed.
resource "terraform_data" "rbac_gate" {
  input = var.secrets_ready
}

# Full SQLAlchemy URL consumed by app/config.py (database_url).
resource "azurerm_key_vault_secret" "db_url" {
  name         = "tf-database-url"
  key_vault_id = var.vault_id
  value        = "postgresql+psycopg://${var.pg_login}:${var.pg_password}@${var.pg_fqdn}:5432/tokenfoundry"
  depends_on   = [terraform_data.rbac_gate]
}

resource "azurerm_key_vault_secret" "jwt" {
  name         = "tf-jwt-secret"
  key_vault_id = var.vault_id
  value        = var.jwt_secret
  depends_on   = [terraform_data.rbac_gate]
}

resource "azurerm_key_vault_secret" "admin_pwd" {
  name         = "tf-admin-password"
  key_vault_id = var.vault_id
  value        = var.admin_password
  depends_on   = [terraform_data.rbac_gate]
}
