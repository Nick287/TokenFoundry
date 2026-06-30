# Key Vault — subscription keys + BYO provider secrets.
# RBAC authorization (not access policies); the Container App's managed identity
# is granted Secrets User / Secrets Officer in the containerapps module.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }
variable "tenant_id" { type = string }
variable "deployer_object_id" {
  type        = string
  description = "Object id of the identity running terraform — granted Secrets Officer so the appsecrets module can write secret values."
}

resource "azurerm_key_vault" "vault" {
  # KV names are globally unique, max 24 chars.
  name                       = substr("${var.name_prefix}kv${var.suffix}", 0, 24)
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tags                       = var.tags
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 7
}

# Grant the deployer (the identity running terraform) Key Vault Secrets Officer.
# Creating the vault is control-plane; writing secret VALUES is data-plane and
# needs this role. Without it the appsecrets module fails 403. Doing it in
# Terraform (rather than a manual `az role assignment`) keeps the whole deploy
# to a single `terraform apply`.
resource "azurerm_role_assignment" "deployer_secrets_officer" {
  scope                = azurerm_key_vault.vault.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = var.deployer_object_id
  principal_type       = "User"
}

# RBAC is eventually consistent: a freshly created assignment isn't usable for a
# minute or two. Pause after granting so the secret writes in appsecrets don't
# race ahead of propagation and hit 403. appsecrets depends on this via the
# secrets_ready output.
resource "time_sleep" "wait_for_rbac" {
  depends_on      = [azurerm_role_assignment.deployer_secrets_officer]
  create_duration = "90s"
}
