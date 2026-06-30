output "apim_name" {
  value = azurerm_api_management.apim.name
}

output "gateway_url" {
  value = azurerm_api_management.apim.gateway_url
}

output "principal_id" {
  value = azurerm_api_management.apim.identity[0].principal_id
}

# apim_id is needed as the azapi parent_id for the backend pool (apim-backends).
output "apim_id" {
  value = azurerm_api_management.apim.id
}
