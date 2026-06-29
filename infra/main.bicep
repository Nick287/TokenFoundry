// Token Foundry — main infrastructure orchestrator.
// Deploy: az deployment group create -g <rg> -f infra/main.bicep -p infra/main.bicepparam
// Preview: az deployment group what-if -g <rg> -f infra/main.bicep -p infra/main.bicepparam

targetScope = 'resourceGroup'

@description('Short name prefix for all resources, e.g. "tokenfoundry"')
param namePrefix string = 'tokenfoundry'

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Environment tag: dev | prod')
param environmentName string = 'dev'

@description('PostgreSQL admin login')
param pgAdminLogin string = 'tfadmin'

@secure()
@description('PostgreSQL admin password')
param pgAdminPassword string

@description('Application container image (aca-app: API + portal in one image)')
param appImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@secure()
@description('HS256 signing secret for self-hosted login JWTs')
param jwtSecret string

@secure()
@description('Seed admin account password')
param adminPassword string

var tags = {
  project: 'token-foundry'
  environment: environmentName
  SecurityControl: 'Ignore'
}

// --- Observability foundation (Log Analytics + App Insights) ---
module monitor 'modules/monitor.bicep' = {
  name: 'monitor'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

// --- Secrets (Key Vault) ---
module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

// --- Metadata DB (PostgreSQL Flexible Server) ---
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

// --- Usage store (Cosmos DB for NoSQL) ---
module cosmos 'modules/cosmos.bicep' = {
  name: 'cosmos'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

// --- Container Registry (holds the single API+portal image) ---
module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

// --- AI gateway (APIM) ---
module apim 'modules/apim.bicep' = {
  name: 'apim'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    appInsightsId: monitor.outputs.appInsightsId
    appInsightsConnectionString: monitor.outputs.appInsightsConnectionString
    cosmosAccountName: cosmos.outputs.accountName
  }
}

// --- Model backends: pool + circuit breaker (preview API version) ---
module apimBackends 'modules/apim-backends.bicep' = {
  name: 'apim-backends'
  params: {
    apimName: apim.outputs.apimName
  }
}

// --- App secrets in Key Vault (DB connection string, JWT secret, admin pwd) ---
module appsecrets 'modules/appsecrets.bicep' = {
  name: 'appsecrets'
  params: {
    vaultName: keyvault.outputs.vaultName
    pgLogin: pgAdminLogin
    pgFqdn: postgres.outputs.serverFqdn
    pgPassword: pgAdminPassword
    jwtSecret: jwtSecret
    adminPassword: adminPassword
  }
}

// --- Container App: single app (API + portal in one image) ---
module containerapps 'modules/containerapps.bicep' = {
  name: 'containerapps'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    logAnalyticsCustomerId: monitor.outputs.logAnalyticsCustomerId
    logAnalyticsKey: monitor.outputs.logAnalyticsPrimaryKey
    appImage: appImage
    keyVaultUri: keyvault.outputs.vaultUri
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosAccountName: cosmos.outputs.accountName
    appInsightsId: monitor.outputs.appInsightsId
    apimServiceName: apim.outputs.apimName
    acrId: acr.outputs.registryId
    acrLoginServer: acr.outputs.loginServer
    vaultName: keyvault.outputs.vaultName
    databaseUrlSecretUri: appsecrets.outputs.databaseUrlSecretUri
    jwtSecretUri: appsecrets.outputs.jwtSecretUri
    adminPasswordSecretUri: appsecrets.outputs.adminPasswordSecretUri
    adminUsername: 'admin'
  }
}

output apimGatewayUrl string = apim.outputs.gatewayUrl
output appFqdn string = containerapps.outputs.appFqdn
output keyVaultUri string = keyvault.outputs.vaultUri
output acrLoginServer string = acr.outputs.loginServer
