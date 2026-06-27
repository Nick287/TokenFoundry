// Token Foundry — LIGHTWEIGHT deployment (no APIM, no Container Apps, no Grafana).
// Validates the fast/cheap data + observability resources first. APIM (slow,
// 30-45 min) and the app tier come in the full deployment via main.bicep.
//
// Deploy: az deployment group create -g <rg> -f infra/main.lite.bicep \
//           -p namePrefix=tokenfoundry -p pgAdminPassword=<pwd>

targetScope = 'resourceGroup'

@description('Short name prefix for all resources')
param namePrefix string = 'tokenfoundry'

@description('Azure region')
param location string = resourceGroup().location

@description('Environment tag')
param environmentName string = 'dev'

@description('PostgreSQL admin login')
param pgAdminLogin string = 'tfadmin'

@secure()
@description('PostgreSQL admin password')
param pgAdminPassword string

var tags = {
  project: 'token-foundry'
  environment: environmentName
}

module monitor 'modules/monitor.bicep' = {
  name: 'monitor'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    adminLogin: pgAdminLogin
    adminPassword: pgAdminPassword
  }
}

module cosmos 'modules/cosmos.bicep' = {
  name: 'cosmos'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module eventhub 'modules/eventhub.bicep' = {
  name: 'eventhub'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

// --- Container Registry (holds the single API+portal image for the full tier) ---
module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

output keyVaultUri string = keyvault.outputs.vaultUri
output postgresFqdn string = postgres.outputs.serverFqdn
output cosmosEndpoint string = cosmos.outputs.endpoint
output appInsightsId string = monitor.outputs.appInsightsId
output eventHubNamespace string = eventhub.outputs.namespaceName
output acrLoginServer string = acr.outputs.loginServer
output acrName string = acr.outputs.registryName
