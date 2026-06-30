# Token Foundry — top-level outputs (mirrors infra/main.bicep outputs).

output "apim_gateway_url" {
  description = "APIM gateway base URL (the GenAI gateway data plane)."
  value       = module.apim.gateway_url
}

output "app_fqdn" {
  description = "Container App public FQDN (API + portal)."
  value       = module.containerapps.app_fqdn
}

output "key_vault_uri" {
  description = "Key Vault URI."
  value       = module.keyvault.vault_uri
}

output "acr_login_server" {
  description = "ACR login server, e.g. myreg.azurecr.io."
  value       = module.acr.login_server
}
