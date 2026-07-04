# Remote-state storage for per-account GitModel hub deployments (方案 A).
#
# Under 方案 A the hub terraform runs in a GitHub Action (Service Principal auth),
# NOT in the control plane and NOT in an ACA Job. So the high-privilege deployer
# identity that P2 needed (subscription Contributor, KV Secrets Officer) is GONE —
# that privilege now lives in the Service Principal held in GitHub repo secrets,
# out of this subscription's terraform entirely.
#
# What remains here is just the shared blob storage that holds every account's
# terraform remote state (key = hubs/<account_id>.tfstate). The Action's SP
# writes state during apply/destroy; the CONTROL PLANE reads it back (outputs:
# app_url, resource_group) via a Storage Blob Data Reader grant made in the
# containerapps module (on the app's system identity).

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }

# --- Remote state storage (shared, one blob per account) ------------------
resource "azurerm_storage_account" "tfstate" {
  # Storage account names: globally unique, 3-24 chars, lowercase alphanumeric.
  name                     = substr("${var.name_prefix}tfstate${var.suffix}", 0, 24)
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = var.tags
  # Shared key stays ENABLED: the azurerm provider manages the blob container
  # (below) via the storage data plane, which requires key auth at create time.
  # The remote-state ACCESS path is still AAD — the Action's `terraform init` uses
  # use_azuread_auth=true under the Service Principal, and the control plane reads
  # the state blob under its system identity (Storage Blob Data Reader, granted in
  # the containerapps module) — so day-to-day state read/write never uses the key.
  shared_access_key_enabled       = true
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "hub-tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}
