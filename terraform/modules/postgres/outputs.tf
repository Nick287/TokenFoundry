output "server_fqdn" {
  value = azurerm_postgresql_flexible_server.pg.fqdn
}

output "database_name" {
  value = azurerm_postgresql_flexible_server_database.db.name
}
