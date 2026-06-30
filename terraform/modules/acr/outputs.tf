output "registry_name" {
  value = azurerm_container_registry.acr.name
}

output "login_server" {
  value = azurerm_container_registry.acr.login_server
}

output "registry_id" {
  value = azurerm_container_registry.acr.id
}
