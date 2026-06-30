# Outputs are versionless secret URIs (references), not secret values — safe to
# output. Use versionless_id (not id): it mirrors the Bicep properties.secretUri,
# which is versionless, so Container App secret references survive rotation.

output "database_url_secret_uri" {
  value = azurerm_key_vault_secret.db_url.versionless_id
}

output "jwt_secret_uri" {
  value = azurerm_key_vault_secret.jwt.versionless_id
}

output "admin_password_secret_uri" {
  value = azurerm_key_vault_secret.admin_pwd.versionless_id
}
