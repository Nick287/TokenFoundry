output "tfstate_storage_account_name" {
  value = azurerm_storage_account.tfstate.name
}

output "tfstate_storage_account_id" {
  description = "Storage account id — the control plane's system identity is granted Storage Blob Data Reader on it (in containerapps) to read per-account terraform outputs from state."
  value       = azurerm_storage_account.tfstate.id
}

output "tfstate_container_name" {
  value = azurerm_storage_container.tfstate.name
}
