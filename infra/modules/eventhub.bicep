// Event Hub — billing-grade usage stream (Phase 2 source of truth).
// Provisioned now so the APIM log-to-eventhub policy has a target.

param namePrefix string
param location string
param tags object

resource namespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: '${namePrefix}-ehns'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 1
  }
}

resource hub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: namespace
  name: 'usage'
  properties: {
    messageRetentionInDays: 1
    partitionCount: 2
  }
}

resource consumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: hub
  name: 'billing'
}

output namespaceName string = namespace.name
output hubName string = hub.name
output consumerGroupName string = consumerGroup.name
