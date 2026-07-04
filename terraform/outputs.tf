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

# --- 方案 A: values scripts/setup-github-deploy.sh feeds to the GitHub Action ---
output "tfstate_storage_account" {
  description = "Storage account holding per-account hub terraform remote state (repo var TFSTATE_STORAGE_ACCOUNT + control-plane TF_TFSTATE_STORAGE_ACCOUNT)."
  value       = module.deployer.tfstate_storage_account_name
}

output "tfstate_container" {
  description = "Blob container for hub terraform remote state (repo var TFSTATE_CONTAINER)."
  value       = module.deployer.tfstate_container_name
}

output "keyvault_name" {
  description = "Key Vault name — the Action reads per-account gh-<id>-jobinput secrets from it (repo var HUB_KEYVAULT_NAME)."
  value       = module.keyvault.vault_name
}
