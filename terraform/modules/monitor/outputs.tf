# NOTE vs Bicep: the Container App Environment in azurerm takes only the
# workspace *id* and resolves the shared key itself, so the Bicep
# logAnalyticsCustomerId / primarySharedKey outputs are intentionally dropped.

output "log_analytics_id" {
  description = "Log Analytics workspace resource id (consumed by the Container App Environment)."
  value       = azurerm_log_analytics_workspace.law.id
}

output "app_insights_id" {
  value = azurerm_application_insights.appi.id
}

output "app_insights_connection_string" {
  value     = azurerm_application_insights.appi.connection_string
  sensitive = true
}
