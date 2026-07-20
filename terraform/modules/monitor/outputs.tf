# NOTE vs Bicep: the Container App Environment in azurerm takes only the
# workspace *id* and resolves the shared key itself, so the Bicep
# primarySharedKey output is intentionally dropped. The customerId (workspace
# GUID) IS surfaced below — the control plane queries the dedicated
# ApiManagementGatewayLlmLog table via query_workspace(customerId), which needs
# it (query_resource against the App Insights component can't see that table).

output "log_analytics_id" {
  description = "Log Analytics workspace resource id (consumed by the Container App Environment)."
  value       = azurerm_log_analytics_workspace.law.id
}

output "log_analytics_customer_id" {
  description = "Log Analytics workspace customerId (GUID) — used by the control plane to query the ApiManagementGatewayLlmLog table for token metering."
  value       = azurerm_log_analytics_workspace.law.workspace_id
}

output "app_insights_id" {
  value = azurerm_application_insights.appi.id
}

output "app_insights_connection_string" {
  value     = azurerm_application_insights.appi.connection_string
  sensitive = true
}
