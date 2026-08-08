# Token Foundry — top-level outputs (mirrors infra/main.bicep outputs).

output "apim_gateway_url" {
  description = "APIM gateway base URL (the GenAI gateway data plane)."
  value       = module.apim.gateway_url
}

output "app_fqdn" {
  description = "Container App public FQDN (API + portal)."
  value       = module.containerapps.app_fqdn
}

# Both consumed by deploy.sh, which rolls the Container App onto the image it
# just built. Terraform sets the image only at CREATE (the field is under
# ignore_changes so update-app.sh's revisions survive an apply), so on a re-run
# the script has to do it — and needs these two to address the resource.
output "app_name" {
  description = "Container App name."
  value       = module.containerapps.app_name
}

output "resource_group" {
  description = "Resource group holding the environment."
  value       = azurerm_resource_group.this.name
}

output "key_vault_uri" {
  description = "Key Vault URI."
  value       = module.keyvault.vault_uri
}

output "acr_login_server" {
  description = "ACR login server, e.g. myreg.azurecr.io."
  value       = module.acr.login_server
}

# --- 方案 A: values the Portal's deploy-config flow feeds to the GitHub Action ---
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

# --- Usage pipeline coordinates the hub deploy needs (repo vars HUB_EVENTHUB_*) ---
output "eventhub_namespace_id" {
  description = "Event Hub namespace id — the hub terraform scopes its Event Hubs Data Sender role assignment here (repo var HUB_EVENTHUB_NAMESPACE_ID)."
  value       = module.eventhub.namespace_id
}

output "eventhub_fqdn" {
  description = "Namespace host each hub's usage producer connects to (repo var HUB_EVENTHUB_FQDN)."
  value       = module.eventhub.fqdn
}

output "eventhub_name" {
  description = "Event hub usage events are sent to (repo var HUB_EVENTHUB_NAME)."
  value       = module.eventhub.eventhub_name
}

output "usage_capture_storage_account" {
  description = "Storage account Event Hub Capture drains usage events into; the control plane's import job reads it."
  value       = module.eventhub.capture_storage_account_name
}

# --- Audit archive coordinates the hub deploy needs (repo vars HUB_AUDIT_*) ---
output "audit_account_url" {
  description = "Blob endpoint hubs write opted-in tenants' raw bodies to (repo var HUB_AUDIT_ACCOUNT_URL)."
  value       = module.audit.account_url
}

output "audit_container" {
  description = "Container audit payloads land in (repo var HUB_AUDIT_CONTAINER)."
  value       = module.audit.container_name
}

output "audit_container_scope" {
  description = "Container resource id — the hub terraform scopes its Storage Blob Data Contributor role assignment here (repo var HUB_AUDIT_CONTAINER_SCOPE)."
  value       = module.audit.container_scope
}

output "audit_storage_account_id" {
  description = "Audit account id. Use this as the scope when granting a NAMED human auditor Storage Blob Data Reader — no service identity is granted read by this terraform, and that is the point."
  value       = module.audit.storage_account_id
}
