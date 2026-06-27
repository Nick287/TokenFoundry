// Cosmos DB for NoSQL — high-write usage records.
// partition key = /pk (subscriptionId_yyyymm); raw records get a TTL.

param namePrefix string
param location string
param tags object

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: take('${namePrefix}-cosmos-${uniqueString(resourceGroup().id)}', 44)
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    disableLocalAuth: true
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: 'tokenfoundry'
  properties: {
    resource: {
      id: 'tokenfoundry'
    }
  }
}

resource usageContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: 'usage'
  properties: {
    resource: {
      id: 'usage'
      partitionKey: {
        paths: [
          '/pk'
        ]
        kind: 'Hash'
      }
      // 90-day TTL on raw records; aggregates are rolled up to PostgreSQL.
      defaultTtl: 7776000
    }
  }
}

output endpoint string = account.properties.documentEndpoint
output accountName string = account.name
