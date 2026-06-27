// Azure Container Registry — holds the single Token Foundry app image
// (API + portal). `az acr build` pushes here; in the full deployment the
// Container App's managed identity pulls via an AcrPull role assignment.

param namePrefix string
param location string
param tags object

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  // ACR names are globally unique, alphanumeric only (no hyphens), 5-50 chars.
  name: take('${namePrefix}acr${uniqueString(resourceGroup().id)}', 50)
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    // RBAC + managed identity only, consistent with Key Vault / Cosmos.
    adminUserEnabled: false
  }
}

output registryName string = acr.name
output loginServer string = acr.properties.loginServer
output registryId string = acr.id
