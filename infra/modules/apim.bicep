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
  name: '${namePrefix}-apim'
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

output apimName string = apim.name
output gatewayUrl string = apim.properties.gatewayUrl
output principalId string = apim.identity.principalId
