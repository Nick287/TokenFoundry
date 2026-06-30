output "vault_name" {
  value = azurerm_key_vault.vault.name
}

output "vault_uri" {
  value = azurerm_key_vault.vault.vault_uri
}

output "vault_id" {
  value = azurerm_key_vault.vault.id
}

# Gate for the appsecrets module: depending on this guarantees the deployer's
# Secrets Officer role is granted AND RBAC has propagated (time_sleep) before any
# secret is written, so the writes don't 403.
output "secrets_ready" {
  value = time_sleep.wait_for_rbac.id
}
