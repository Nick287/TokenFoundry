output "app_fqdn" {
  value = azurerm_container_app.app.ingress[0].fqdn
}

# deploy.sh rolls the app onto the image it just built, because terraform no
# longer owns that field (see the lifecycle block in main.tf). It therefore
# needs the app's name, which it has no other way to know.
output "app_name" {
  value = azurerm_container_app.app.name
}

output "app_principal_id" {
  value = azurerm_container_app.app.identity[0].principal_id
}
