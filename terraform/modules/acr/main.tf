# Azure Container Registry — holds the single Token Foundry app image
# (API + portal). `az acr build` pushes here; the Container App's user-assigned
# identity pulls via an AcrPull role assignment (granted in containerapps).

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }

resource "azurerm_container_registry" "acr" {
  # ACR names are globally unique, alphanumeric only (no hyphens), 5-50 chars.
  name                = substr("${var.name_prefix}acr${var.suffix}", 0, 50)
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  sku                 = "Basic"
  # RBAC + managed identity only, consistent with Key Vault / Cosmos.
  admin_enabled = false
}
