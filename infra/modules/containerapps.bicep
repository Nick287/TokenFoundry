// Container Apps environment + ONE app (aca-app): a single image serving both
// the FastAPI API and the built React portal (no nginx, no second container).
// Its system-assigned identity is what the control plane uses for Key Vault /
// Cosmos / APIM management (DefaultAzureCredential).

param namePrefix string
param location string
param tags object
param logAnalyticsCustomerId string

@secure()
param logAnalyticsKey string

param appImage string
param keyVaultUri string
param cosmosEndpoint string
param apimServiceName string

@description('ACR resource id the app pulls its image from')
param acrId string

@description('ACR login server, e.g. myreg.azurecr.io')
param acrLoginServer string

@description('Key Vault name backing the secret references')
param vaultName string

@description('Key Vault secret URI for the database connection string')
param databaseUrlSecretUri string

@description('Key Vault secret URI for the JWT signing secret')
param jwtSecretUri string

@description('Key Vault secret URI for the seed admin password')
param adminPasswordSecretUri string

@description('Seed admin username (non-secret)')
param adminUsername string = 'admin'

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-cae'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsKey
      }
    }
  }
}

// --- User-assigned identity dedicated to pulling from ACR ---
// A UAMI's principalId is known at create time, so AcrPull can be granted
// BEFORE the app exists — avoiding the system-identity chicken-and-egg where
// the app tries to pull before its own identity has been granted the role.
resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-acrpull-id'
  location: location
  tags: tags
}

// Grant the pull identity AcrPull on the registry, before the app is created.
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: last(split(acrId, '/'))
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acrId, pullIdentity.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  properties: {
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    // AcrPull
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

// Grant the pull identity Key Vault Secrets User so the platform can resolve
// the secret references below at startup. Pre-granted (like AcrPull) so it's in
// place before the app's first revision activates.
resource vault 'Microsoft.KeyVault/vaults@2024-04-01-preview' existing = {
  name: vaultName
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, pullIdentity.id, '4633458b-17de-408a-b874-0445c86b69e6')
  properties: {
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    // Key Vault Secrets User
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

// --- Single app: API + portal in one image ---
resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-aca-app'
  location: location
  tags: tags
  // SystemAssigned: runtime access to Key Vault / Cosmos / APIM (DefaultAzureCredential).
  // UserAssigned: pulls the image from the private ACR (role pre-granted above).
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  // Ensure the pull identity holds AcrPull + KV Secrets User before the first
  // revision activates (mirrors the ACR race fix).
  dependsOn: [
    acrPull
    kvSecretsUser
  ]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      // Pull from the private ACR using the dedicated user-assigned identity.
      registries: [
        {
          server: acrLoginServer
          identity: pullIdentity.id
        }
      ]
      // Key Vault secret references resolved via the pull identity.
      secrets: [
        {
          name: 'tf-database-url'
          keyVaultUrl: databaseUrlSecretUri
          identity: pullIdentity.id
        }
        {
          name: 'tf-jwt-secret'
          keyVaultUrl: jwtSecretUri
          identity: pullIdentity.id
        }
        {
          name: 'tf-admin-password'
          keyVaultUrl: adminPasswordSecretUri
          identity: pullIdentity.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
    }
    template: {
      containers: [
        {
          name: 'app'
          image: appImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'TF_KEYVAULT_URI', value: keyVaultUri }
            { name: 'TF_COSMOS_ENDPOINT', value: cosmosEndpoint }
            { name: 'TF_APIM_SERVICE_NAME', value: apimServiceName }
            { name: 'TF_RESOURCE_GROUP', value: resourceGroup().name }
            { name: 'TF_AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
            { name: 'TF_ENVIRONMENT', value: 'prod' }
            // Self-hosted login + DB connection (secrets from Key Vault).
            { name: 'TF_DATABASE_URL', secretRef: 'tf-database-url' }
            { name: 'TF_JWT_SECRET', secretRef: 'tf-jwt-secret' }
            { name: 'TF_ADMIN_PASSWORD', secretRef: 'tf-admin-password' }
            { name: 'TF_ADMIN_USERNAME', value: adminUsername }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output appFqdn string = app.properties.configuration.ingress.fqdn
output appPrincipalId string = app.identity.principalId

// Grant the app's SYSTEM identity rights to manage APIM (create subscriptions /
// products / backends at runtime via DefaultAzureCredential). Unlike the ACR
// pull, this is needed only while the app runs, so the system identity (which
// exists only after the app is created) is fine — no startup race.
resource apimService 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

resource apimContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: apimService
  name: guid(apimService.id, app.id, '312a565d-c81f-4fd8-895a-4e21e48d571c')
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    // API Management Service Contributor
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '312a565d-c81f-4fd8-895a-4e21e48d571c'
    )
  }
}

// Grant the app's SYSTEM identity read/WRITE on Key Vault. The control plane
// (keyvault.py via DefaultAzureCredential = system identity) WRITES subscription
// keys + BYO secrets at runtime, so it needs Secrets Officer, not just the
// read-only Secrets User the pull identity has for resolving secret refs.
resource kvSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, app.id, 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    // Key Vault Secrets Officer (read/write secrets)
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
    )
  }
}
