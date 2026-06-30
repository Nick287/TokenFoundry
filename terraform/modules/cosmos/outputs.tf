output "endpoint" {
  value = azurerm_cosmosdb_account.account.endpoint
}

output "account_name" {
  value = azurerm_cosmosdb_account.account.name
}

# account_id is needed (not just the name) to build the data-plane
# sqlRoleDefinitions id in the apim + containerapps modules.
output "account_id" {
  value = azurerm_cosmosdb_account.account.id
}
