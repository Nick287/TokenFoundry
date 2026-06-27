// APIM backend pool + circuit breaker — the GenAI gateway resilience features.
//
// These use the PREVIEW API version (2023-09-01-preview), which Bicep supports
// natively with no provider lag — the reason we chose Bicep over Terraform for
// IaC. This module shows the canonical shape; real per-tenant/per-provider
// backends are added at runtime by the FastAPI provisioner.
//
// The example provisions two placeholder Single backends with circuit breakers,
// then a Pool backend load-balancing across them (priority/weight).

param apimName string

@description('Example backend endpoints to pool. Replace/extend at runtime via provisioner.')
param backendUrls array = [
  'https://example-aoai-1.openai.azure.com/openai'
  'https://example-aoai-2.openai.azure.com/openai'
]

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

// Single backends with circuit breaker rules (trip on 5xx, honor Retry-After).
resource singleBackends 'Microsoft.ApiManagement/service/backends@2023-09-01-preview' = [
  for (url, i) in backendUrls: {
    parent: apim
    name: 'pooled-backend-${i}'
    properties: {
      url: url
      protocol: 'http'
      circuitBreaker: {
        rules: [
          {
            name: 'trip-on-5xx'
            failureCondition: {
              count: 3
              interval: 'PT1H'
              statusCodeRanges: [
                {
                  min: 500
                  max: 599
                }
              ]
              errorReasons: [
                'Server errors'
              ]
            }
            tripDuration: 'PT1H'
            acceptRetryAfter: true
          }
        ]
      }
    }
  }
]

// Load-balanced pool across the single backends (round-robin by default).
resource backendPool 'Microsoft.ApiManagement/service/backends@2023-09-01-preview' = {
  parent: apim
  name: 'llm-pool'
  properties: {
    description: 'Load-balanced pool for pooled LLM backends'
    type: 'Pool'
    pool: {
      services: [
        for (url, i) in backendUrls: {
          id: '${apim.id}/backends/pooled-backend-${i}'
          priority: 1
          weight: 1
        }
      ]
    }
  }
  dependsOn: [
    singleBackends
  ]
}

output poolBackendId string = backendPool.id
output poolBackendName string = backendPool.name
