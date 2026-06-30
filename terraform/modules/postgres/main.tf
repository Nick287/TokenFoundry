# PostgreSQL Flexible Server — control-plane metadata store.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }
variable "admin_login" { type = string }
variable "admin_password" {
  type      = string
  sensitive = true
}

resource "azurerm_postgresql_flexible_server" "pg" {
  name                = substr("${var.name_prefix}-pg-${var.suffix}", 0, 60)
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  version             = "16"
  # azurerm prefixes the tier: Burstable -> B_. (Bicep used Standard_B1ms +
  # tier: Burstable; azurerm packs both into B_Standard_B1ms.)
  sku_name                     = "B_Standard_B1ms"
  storage_mb                   = 32768
  backup_retention_days        = 7
  geo_redundant_backup_enabled = false
  administrator_login          = var.admin_login
  administrator_password       = var.admin_password
  # HA disabled = omit the high_availability block entirely.

  # Azure auto-assigns an availability zone at create time. Without HA there's
  # no standby zone to "exchange" with, so azurerm's attempt to manage `zone`
  # on later applies is rejected (zone is immutable post-create) and aborts the
  # run. Ignore it.
  lifecycle {
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "db" {
  name      = "tokenfoundry"
  server_id = azurerm_postgresql_flexible_server.pg.id
}

# Allow Azure services (Container Apps) to reach the server. Tighten to VNet
# integration in prod.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure" {
  name             = "AllowAzureServices"
  server_id        = azurerm_postgresql_flexible_server.pg.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
