output "namespace_id" {
  description = "Namespace resource id — scope for the `Azure Event Hubs Data Sender` role each hub's managed identity is granted (in the hub's own terraform)."
  value       = azurerm_eventhub_namespace.ns.id
}

output "namespace_name" {
  value = azurerm_eventhub_namespace.ns.name
}

output "fqdn" {
  description = "Namespace host the hub's producer connects to (TF_EVENTHUB_FQDN). Composed from the name rather than parsed out of a connection string, because local auth is disabled on the namespace and no connection string is issued."
  value       = "${azurerm_eventhub_namespace.ns.name}.servicebus.windows.net"
}

output "eventhub_name" {
  description = "Event hub (topic) name usage events are sent to (TF_EVENTHUB_NAME)."
  value       = azurerm_eventhub.usage.name
}

output "capture_storage_account_name" {
  value = azurerm_storage_account.capture.name
}

output "capture_storage_account_id" {
  description = "Capture storage id — the control plane's system identity is granted Storage Blob Data Reader on it (in containerapps) so the import job can read Avro blobs."
  value       = azurerm_storage_account.capture.id
}

output "capture_container_name" {
  value = azurerm_storage_container.capture.name
}

output "capture_interval_seconds" {
  description = "Echoed so the control plane can schedule the import job no more often than Capture actually produces blobs."
  value       = var.capture_interval_seconds
}
