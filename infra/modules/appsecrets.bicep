// App secrets — writes the runtime secrets the control plane reads from Key
// Vault: the PostgreSQL connection string (assembled here so the password never
// lands in app settings), the JWT signing secret, and the seed admin password.
//
// The Container App's pull identity is granted Key Vault Secrets User (in
// containerapps.bicep) and resolves these via secret references at startup.

param vaultName string

@description('PostgreSQL admin login')
param pgLogin string

@description('PostgreSQL server FQDN')
param pgFqdn string

@secure()
@description('PostgreSQL admin password')
param pgPassword string

@secure()
@description('HS256 signing secret for self-hosted login JWTs')
param jwtSecret string

@secure()
@description('Seed admin account password')
param adminPassword string

resource vault 'Microsoft.KeyVault/vaults@2024-04-01-preview' existing = {
  name: vaultName
}

// Full SQLAlchemy URL consumed by app/config.py (database_url).
var databaseUrl = 'postgresql+psycopg://${pgLogin}:${pgPassword}@${pgFqdn}:5432/tokenfoundry'

resource dbUrlSecret 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: vault
  name: 'tf-database-url'
  properties: {
    value: databaseUrl
  }
}

resource jwtSecretRes 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: vault
  name: 'tf-jwt-secret'
  properties: {
    value: jwtSecret
  }
}

resource adminPwdSecret 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: vault
  name: 'tf-admin-password'
  properties: {
    value: adminPassword
  }
}

// These are secret URIs (references), not secret values — safe to output.
#disable-next-line outputs-should-not-contain-secrets
output databaseUrlSecretUri string = dbUrlSecret.properties.secretUri
#disable-next-line outputs-should-not-contain-secrets
output jwtSecretUri string = jwtSecretRes.properties.secretUri
#disable-next-line outputs-should-not-contain-secrets
output adminPasswordSecretUri string = adminPwdSecret.properties.secretUri
