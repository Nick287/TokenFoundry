// API Management — the GenAI gateway (data plane).
// Developer SKU for MVP; system-assigned identity used to reach AI backends and
// to be granted Cognitive Services User on pooled Azure OpenAI deployments.

param namePrefix string
param location string
param tags object
param appInsightsId string
param appInsightsConnectionString string

@description('Cosmos DB account name — APIM writes usage records to it directly (outbound policy, MI auth)')
param cosmosAccountName string

@description('Publisher email for APIM')
param publisherEmail string = 'admin@tokenfoundry.local'

@description('Publisher org name for APIM')
param publisherName string = 'Token Foundry'

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: take('${namePrefix}-apim-${uniqueString(resourceGroup().id)}', 50)
  location: location
  tags: tags
  sku: {
    name: 'Developer'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// Wire APIM telemetry into Application Insights (token metrics, request logs).
resource apimLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsightsId
    credentials: {
      connectionString: appInsightsConnectionString
    }
  }
}

// Service-level diagnostic: this is what actually emits per-request telemetry
// (requests + backend dependencies, each with a duration) to the logger above.
// The logger alone only connects the pipe; without a diagnostic, APIM sends the
// custom token metric (emit-token-metric policy) but NOT request/latency logs.
//
// Sampling note (this is the knob that controls cost vs. detail — it has NO
// effect on token billing, which rides a separate custom-metric path):
//   * percentage 100  -> every request logged. Right for MVP/debugging; lets you
//                        inspect any single slow call. Cheap at low volume.
//   * percentage 5-20 -> log a random subset at scale. APIM uses *fixed/probabilistic*
//                        sampling, so P50/P95/P99 latency stays statistically
//                        accurate; you only lose the ability to find one specific
//                        request's trace (it may have been dropped). Cuts Log
//                        Analytics ingestion cost proportionally.
resource apimDiagnostic 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
  parent: apim
  name: 'applicationinsights' // must be this exact name to bind to App Insights
  properties: {
    loggerId: apimLogger.id
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    alwaysLog: 'allErrors'
    verbosity: 'information'
    httpCorrelationProtocol: 'W3C'
  }
}

output apimName string = apim.name
output gatewayUrl string = apim.properties.gatewayUrl
output principalId string = apim.identity.principalId

// Grant APIM's system identity Cosmos DB data-plane write access. The outbound
// policy (apim/policies/outbound-cosmos-write.xml) uses this identity to write a
// usage record per LLM call directly to the `usage` container via the Cosmos
// REST API (type=aad auth). The account sets disableLocalAuth=true, so this
// data-plane RBAC assignment is required — control-plane roles do NOT grant it.
// Built-in "Cosmos DB Data Contributor" (…0002) covers item create/upsert.
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource apimCosmosWriter 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, apim.id, '00000000-0000-0000-0000-000000000002')
  properties: {
    principalId: apim.identity.principalId
    roleDefinitionId: resourceId(
      'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions',
      cosmosAccountName,
      '00000000-0000-0000-0000-000000000002'
    )
    scope: cosmosAccount.id
  }
}
