// Key Vault — subscription keys + BYO provider secrets.
// RBAC authorization (not access policies); the Container App's managed identity
// is granted Secrets User at the containerapps module.

param namePrefix string
param location string
param tags object

resource vault 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  // KV names are globally unique, max 24 chars
  name: take('${namePrefix}kv${uniqueString(resourceGroup().id)}', 24)
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
output vaultId string = vault.id
