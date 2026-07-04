output "app_url" {
  description = "Public HTTPS URL of the GitModel Hub."
  value       = "https://${azurerm_container_app.hub.ingress[0].fqdn}"
}

output "resource_group" {
  value = azurerm_resource_group.rg.name
}

output "acr_login_server" {
  description = "Login server of the container registry used by this app."
  value       = local.acr_login_server
}

output "image" {
  description = "Image repository:tag (tag is the source content hash) built and deployed."
  value       = local.image_ref
}
