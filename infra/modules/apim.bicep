// API Management — the GenAI gateway (data plane).
// Developer SKU for MVP; system-assigned identity used to reach AI backends and
// to be granted Cognitive Services User on pooled Azure OpenAI deployments.

param namePrefix string
param location string
param tags object
param appInsightsId string
param appInsightsConnectionString string

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

// NOTE: APIM's identity deliberately has NO Cosmos role. It held "Cosmos DB Data
// Contributor" for an outbound policy that wrote one usage document per call.
// That policy is gone — usage now travels hub -> Event Hub -> Capture -> import
// job -> Cosmos, and the gateway never touches the billing store. The grant was
// standing write access to every tenant's billing data held by a component with
// no reason to reach it, so it is removed rather than left as "harmless".
