output "app_fqdn" {
  value = azurerm_container_app.app.ingress[0].fqdn
}

output "app_principal_id" {
  value = azurerm_container_app.app.identity[0].principal_id
}
