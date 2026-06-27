// Azure Managed Grafana — read-only observability dashboards.
// Connects to Log Analytics + App Insights as data sources (RBAC granted out of
// band: grant the Grafana MI "Monitoring Reader" on the subscription/RG).

param namePrefix string
param location string
param tags object

resource grafana 'Microsoft.Dashboard/grafana@2023-09-01' = {
  name: '${namePrefix}-grafana'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    apiKey: 'Disabled'
    grafanaMajorVersion: '12'
    publicNetworkAccess: 'Enabled'
  }
}

output endpoint string = grafana.properties.endpoint
output principalId string = grafana.identity.principalId
output grafanaName string = grafana.name

// Grant Grafana's managed identity Monitoring Reader on this resource group so
// its dashboards can query Azure Monitor / Log Analytics / App Insights (the
// token + request-latency panels). Previously this was a manual "out of band"
// step; without it every panel renders "No data" even though the telemetry
// exists. Scoped to the RG since all observability data lives here.
resource monitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: resourceGroup()
  name: guid(resourceGroup().id, grafana.id, '43d0d8ad-25c7-4714-9337-8ba259a9fe05')
  properties: {
    principalId: grafana.identity.principalId
    principalType: 'ServicePrincipal'
    // Monitoring Reader
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
    )
  }
}
