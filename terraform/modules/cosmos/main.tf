# Cosmos DB for NoSQL — high-write usage records.
# partition key = /pk (subscriptionId_yyyymm); raw records get a 90-day TTL.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }

resource "azurerm_cosmosdb_account" "account" {
  name                = substr("${var.name_prefix}-cosmos-${var.suffix}", 0, 44)
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  # No keys — data-plane access is AAD/RBAC only (mirrors Bicep
  # disableLocalAuth=true). The app + APIM identities get data-plane role
  # assignments in their respective modules. (azurerm v4 renamed the old
  # local_authentication_disabled=true to local_authentication_enabled=false.)
  local_authentication_enabled = false

  consistency_policy {
    consistency_level = "Session"
  }

  capabilities {
    name = "EnableServerless"
  }

  geo_location {
    location          = var.location
    failover_priority = 0
  }
}

resource "azurerm_cosmosdb_sql_database" "db" {
  name                = "tokenfoundry"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.account.name
}

resource "azurerm_cosmosdb_sql_container" "usage" {
  name                = "usage"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.account.name
  database_name       = azurerm_cosmosdb_sql_database.db.name
  partition_key_paths = ["/pk"]
  # 90-day TTL on raw records; aggregates are rolled up to PostgreSQL.
  default_ttl = 7776000
}
