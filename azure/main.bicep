targetScope = 'resourceGroup'

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Azure region for PostgreSQL Flexible Server (override if location restricts Postgres)')
param postgresLocation string = location

@description('Short lowercase alphanumeric prefix for Azure resource names')
@minLength(3)
@maxLength(12)
param namePrefix string = 'shoppie'

@description('Container image tag to deploy')
param imageTag string = 'latest'

@description('Set false on initial run before images are pushed to ACR')
param deployApps bool = true

@description('PostgreSQL Flexible Server administrator login')
param postgresAdminUser string = 'shoppieadmin'

@description('PostgreSQL Flexible Server administrator password')
@secure()
param postgresAdminPassword string

@description('OpenRouter API key used by the Shoppie Agent')
@secure()
param openrouterApiKey string

@description('Shared API key (at least 32 characters) between backend and agent')
@secure()
@minLength(32)
param undercoverApiKey string

@description('Optional model override (blank uses deepseek/deepseek-v4.1-flash)')
param modelName string = ''

@description('PostgreSQL database name')
param databaseName string = 'shoppie'

var suffix = uniqueString(resourceGroup().id, namePrefix)
var acrName = toLower('${namePrefix}${suffix}acr')
var pgName = toLower('${namePrefix}-${suffix}-pg')
var envName = '${namePrefix}-${suffix}-env'
var logsName = '${namePrefix}-${suffix}-logs'
var identityName = '${namePrefix}-${suffix}-acrpull'

var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, pullIdentity.id, acrPullRoleId)
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: pgName
  location: postgresLocation
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminUser
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

resource postgresDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: postgres
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource allowAzureIps 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: postgres
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

var databaseUrl = 'postgresql://${uriComponent(postgresAdminUser)}:${uriComponent(postgresAdminPassword)}@${postgres.properties.fullyQualifiedDomainName}:5432/${databaseName}?sslmode=require'
var backendFqdn = '${namePrefix}-backend.${containerEnv.properties.defaultDomain}'
var agentInternalFqdn = '${namePrefix}-agent.internal.${containerEnv.properties.defaultDomain}'
var contextInternalFqdn = '${namePrefix}-context.internal.${containerEnv.properties.defaultDomain}'

resource contextApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${namePrefix}-context'
  location: location
  dependsOn: [
    acrPullRole
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      registries: [
        {
          server: acr.properties.loginServer
          identity: pullIdentity.id
        }
      ]
      ingress: {
        external: false
        targetPort: 8006
        transport: 'http'
      }
    }
    template: {
      containers: [
        {
          name: 'context'
          image: '${acr.properties.loginServer}/shoppie-context:${imageTag}'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'HOST'
              value: '0.0.0.0'
            }
            {
              name: 'PORT'
              value: '8006'
            }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8006
              }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

resource backendApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${namePrefix}-backend'
  location: location
  dependsOn: [
    acrPullRole
    postgresDb
    allowAzureIps
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      registries: [
        {
          server: acr.properties.loginServer
          identity: pullIdentity.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
      }
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'undercover-api-key'
          value: undercoverApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: '${acr.properties.loginServer}/shoppie-backend:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'UNDERCOVER_API_KEY'
              secretRef: 'undercover-api-key'
            }
            {
              name: 'UNDERCOVER_AGENT'
              value: 'https://${agentInternalFqdn}'
            }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/health'
                port: 8000
              }
              periodSeconds: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/health'
                port: 8000
              }
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

resource agentApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${namePrefix}-agent'
  location: location
  dependsOn: [
    acrPullRole
    postgresDb
    allowAzureIps
    contextApp
    backendApp
  ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      registries: [
        {
          server: acr.properties.loginServer
          identity: pullIdentity.id
        }
      ]
      ingress: {
        external: false
        targetPort: 8080
        transport: 'http'
      }
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'openrouter-api-key'
          value: openrouterApiKey
        }
        {
          name: 'undercover-api-key'
          value: undercoverApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: '${acr.properties.loginServer}/shoppie-agent:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(
            [
              {
                name: 'DATABASE_URL'
                secretRef: 'database-url'
              }
              {
                name: 'OPENROUTER_API_KEY'
                secretRef: 'openrouter-api-key'
              }
              {
                name: 'UNDERCOVER_API_KEY'
                secretRef: 'undercover-api-key'
              }
              {
                name: 'UNDERCOVER_API'
                value: 'https://${backendFqdn}'
              }
              {
                name: 'UNDERCOVER_CONTEXT'
                value: 'https://${contextInternalFqdn}/mcp'
              }
            ],
            empty(modelName)
              ? []
              : [
                  {
                    name: 'MODEL_NAME'
                    value: modelName
                  }
                ]
          )
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/ping'
                port: 8080
              }
              periodSeconds: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/ping'
                port: 8080
              }
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output backendUrl string = 'https://${backendFqdn}'
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output postgresHost string = postgres.properties.fullyQualifiedDomainName
