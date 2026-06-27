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
