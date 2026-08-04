output "account_url" {
  description = "Blob endpoint each hub's audit writer targets (TF_AUDIT_ACCOUNT_URL). Setting this — together with the container — is what turns the hub's audit path on at all; an unset value makes hub/audit.py a no-op."
  value       = azurerm_storage_account.audit.primary_blob_endpoint
}

output "container_name" {
  description = "Container raw payloads are written to (TF_AUDIT_CONTAINER)."
  value       = azurerm_storage_container.audit.name
}

output "container_scope" {
  description = "Resource id of the container — the scope on which each hub's managed identity is granted Storage Blob Data Contributor (in the hub's own terraform). Container-scoped, not account-scoped, so a hub can write payloads and touch nothing else in the account."
  # Composed rather than read off the resource: azurerm 4.80 deprecates
  # `resource_manager_id` in favour of `id`, whose format depends on whether the
  # container was declared with `storage_account_id` or the legacy account name.
  # A role-assignment scope is not a place to be approximately right — a wrong
  # string here fails the apply, and a right-looking-but-broader one would grant
  # the hub the whole account. This is the documented ARM path for a container.
  value = "${azurerm_storage_account.audit.id}/blobServices/default/containers/${azurerm_storage_container.audit.name}"
}

output "storage_account_id" {
  description = "Account id. Exported for granting a named human auditor Storage Blob Data Reader — deliberately NOT granted to any service by this terraform."
  value       = azurerm_storage_account.audit.id
}

output "retention_days" {
  description = "Echoed so the control plane can state the real retention window instead of a hard-coded one drifting out of sync with infra."
  value       = var.retention_days
}
