// =============================================================================
// Nesq Bot - Azure infrastructure
// =============================================================================
// Provisions the whole stack from README.md:
//   Container Apps (api + worker + self-hosted Temporal), PostgreSQL Flexible
//   Server with pgvector, Redis, Key Vault, Blob + Files,
//   Log Analytics + Application Insights, Azure OpenAI (Foundry) deployments,
//   ACR, a VNet whose desktop subnet is delegated to Microsoft.ContainerInstance,
//   a user-assigned managed identity with least-privilege RBAC, and optionally
//   AKS for the Bot Desktop pool (off; ACI is the supported path).
//
// Two axes, deliberately independent:
//   environmentName  what to HARDEN (auth, retention, purge protection, CORS)
//   sizingTier       what to SPEND  (Postgres/Redis SKUs, replicas, CPU)
// environmentName='prod' + sizingTier='lean' is the supported production shape
// for an internal team: production behaviour on burstable hardware.
//
// Deploy:
//   az group create -n rg-nesqbot-dev -l swedencentral
//   az deployment group create -g rg-nesqbot-dev \
//     -f infra/azure/main.bicep -p infra/azure/main.bicepparam
//
// What-if first (CI runs this on every infra PR):
//   az deployment group what-if -g rg-nesqbot-dev \
//     -f infra/azure/main.bicep -p infra/azure/main.bicepparam
// =============================================================================

targetScope = 'resourceGroup'

// --------------------------------------------------------------------------
// Parameters
// --------------------------------------------------------------------------

@description('Environment name. Drives resource NAMES and HARDENING only - never sizing. prod turns on Key Vault purge protection, 90 day log retention, longer backup and soft-delete windows, and drops localhost from the API CORS allow-list. Use sizingTier for anything that costs money.')
@allowed([
  'dev'
  'staging'
  'prod'
])
param environmentName string = 'dev'

@description('How much hardware to buy. lean = Burstable B2s Postgres, Redis as a single-replica Container App, single-replica apps - the supported production shape for an internal team. full = GeneralPurpose zone-redundant Postgres, managed Redis Enterprise, replica floors of 2. Independent of environmentName by design: production hardening must not drag production-scale spend along with it.')
@allowed([
  'lean'
  'full'
])
param sizingTier string = 'lean'

@description('Azure region for every resource. Foundry model availability varies by region; swedencentral carries the gpt-5.6 family and keeps data in the EU, which is the point. Verify with: az cognitiveservices model list -l swedencentral')
param location string = resourceGroup().location

@description('Deterministic suffix for globally unique names. Override only to pin names across resource groups.')
@minLength(4)
@maxLength(13)
param suffix string = uniqueString(resourceGroup().id)

@description('Postgres administrator login. Cannot be azure_superuser, admin, administrator, root, guest or public.')
param postgresAdminLogin string = 'pgadmin'

@secure()
@minLength(16)
@description('Postgres administrator password. Pass at deploy time; never commit it.')
param postgresAdminPassword string

@secure()
@minLength(32)
@description('Signing key for the API JWTs. Generate: python -c "import secrets;print(secrets.token_urlsafe(48))"')
param jwtSecret string

@secure()
@minLength(32)
@description('Bearer token the worker presents to the API. Generate the same way as jwtSecret.')
param workerApiToken string

@secure()
@minLength(24)
@description('Shared secret for the Bot Desktop sidecar (X-Nesq-Sidecar-Token).')
param sidecarToken string

@description('Entra tenant used for user sign-in. Defaults to the deploying tenant.')
param entraTenantId string = subscription().tenantId

@description('Entra app registration (client) id for the desktop/mobile sign-in flow. The registration itself is a manual step - app registrations are Microsoft Graph objects, not ARM resources. It needs the "Mobile and desktop applications" platform, redirect URI nesqbot://auth, and ID tokens enabled. See infra/azure/README.md. Leave empty to wire it up post-deploy.')
param entraClientId string = ''

@description('App id of the "Nesq Bot API" registration - the audience the API accepts on an access token. Distinct from entraClientId, which is the public desktop/mobile client. Both are issued by this tenant and verify against the same JWKS, so swapping them makes the API accept the wrong audience without any error. Empty falls back to the managed identity for SDK auth only.')
param entraApiAppId string = ''

@description('Delegated scope the API requires in the token `scp` claim. Must match the scope exposed by the API registration.')
param entraApiScope string = 'access_as_user'

@description('Redirect URI the mobile and desktop clients register with Entra. Surfaced as an output so the mobile Settings screen and the registration match exactly.')
param clientRedirectUri string = 'nesqbot://auth'

@description('Deploy a self-hosted Temporal server as a Container App with internal-only ingress, backed by its own databases on the Postgres Flexible Server below. Keeps workflow state in-tenant and in-region instead of shipping it to Temporal Cloud.')
param deployTemporal bool = true

@description('Pinned Redis image for the lean tier. Matches docker-compose.yml. 7.4-alpine rather than 7-alpine because the latter floats across minor versions, which is not a pin.')
param redisImage string = 'redis:7.4-alpine'

@description('Pinned Temporal server image. Matches docker-compose.yml so dev and prod run the same server version - a drift here is how you discover a schema move in production.')
param temporalImage string = 'temporalio/auto-setup:1.25.2'

@description('Override the Temporal endpoint (host:port). Leave empty to use the self-hosted Container App when deployTemporal is true, or set it to point at Temporal Cloud instead.')
param temporalHost string = ''

@description('Temporal namespace.')
param temporalNamespace string = 'default'

@description('Workflow history retention for the auto-created namespace.')
param temporalNamespaceRetention string = '720h'

@description('Temporal history shard count. IMMUTABLE once the schema is set up - changing it later means a fresh cluster and a namespace migration, not a redeploy. 128 is sized for a burstable Postgres; Temporal suggests 512 for clusters that will actually see that load.')
@minValue(1)
@maxValue(4096)
param temporalHistoryShards int = 128

@description('Where the API runs Bot Desktops. aci is the supported production path: one hypervisor-isolated container group per bot, billed per second, no cluster floor. mock needs no infrastructure at all.')
@allowed([
  'mock'
  'aci'
  'aks'
])
param botDesktopMode string = 'aci'

@description('vCPU per Bot Desktop container group.')
param aciCpu int = 2

@description('Memory (GiB) per Bot Desktop container group. A desktop asks for 1Gi and can burn 3Gi.')
param aciMemoryGb int = 4

@description('Address space for the VNet carrying the Container Apps environment and the ACI desktop subnet. Must not overlap anything you plan to peer with.')
param vnetAddressPrefix string = '10.60.0.0/16'

@description('Infrastructure subnet for the Container Apps environment, delegated to Microsoft.App/environments. /23 is the documented recommendation for a workload-profiles environment; /27 is the hard floor.')
param containerAppsSubnetPrefix string = '10.60.0.0/23'

@description('Subnet the Bot Desktop container groups are injected into, delegated to Microsoft.ContainerInstance/containerGroups. Desktops get a private IP and no public endpoint.')
param aciSubnetPrefix string = '10.60.4.0/23'

@description('Give the Bot Desktop subnet a NAT gateway. REQUIRED for ACI: since Azure retired default outbound access on 2025-09-30 a new subnet has no implicit route to the internet, so without this the image pull from ACR hangs until the container group times out - and a desktop with no internet cannot sign into anything either. Turn it off only if you have attached your own egress.')
param deployAciNatGateway bool = true

@description('Exact origins allowed to call the API from a browser. Empty derives a default: prod gets the published hostnames only, everything else also gets the Tauri and Expo dev servers.')
param apiAllowedOrigins array = []

@description('Provision AKS for the Bot Desktop pool. Off, and staying off: ACI does the same job without a node floor. Kept so the template does not lose the shape of it.')
param deployBotDesktopAks bool = false

@description('Node count for the Bot Desktop user pool when deployBotDesktopAks is true.')
@minValue(1)
@maxValue(20)
param botDesktopNodeCount int = 1

@description('VM size for the Bot Desktop pool. A desktop asks for 1Gi and can burn 3Gi.')
param botDesktopNodeSize string = 'Standard_D4as_v5'

@description('Container image tag deployed to the API and worker Container Apps. Never latest.')
param imageTag string = 'v0.1.0'

@description('Overrides imageTag for the API image only. The three images are versioned independently in practice - a hotfix to the API should not force the worker and the 1.6 GB bot-desktop image to be rebuilt and re-pushed just to keep one shared tag honest. Empty falls back to imageTag.')
param apiImageTag string = ''

@description('Overrides imageTag for the worker image only. Empty falls back to imageTag.')
param workerImageTag string = ''

@description('Overrides imageTag for the bot-desktop image only; empty falls back to imageTag. Unlike the other two this carries a concrete default rather than \'\', because the API has a hard floor here: the browser_* agent tools call the sidecar\'s /browser/* CDP surface, which does not exist before v0.2.0. Against an older desktop every one of them answers 503 browser_unavailable and the agent silently falls back to driving the screen by pixel coordinates - safe, and exactly the behaviour this pin exists to stop.')
param botDesktopImageTag string = 'v0.2.0'

// Resolved once so every reference below agrees. Before this existed a single
// tag drove all three, while production ran api:v0.1.3 against worker:v0.1.0 -
// so *any* redeploy was going to move one of them, and passing the API's tag
// would have silently rolled the worker forward to an image that does not
// exist, while passing the worker's would have rolled the API back onto the
// build that mocks every model call.
// botDesktopImageTag defaults to a real tag rather than to '', so this ternary
// only reaches imageTag when a caller passes '' explicitly. That asymmetry is
// deliberate: the API's browser_* tools require the /browser/* CDP surface the
// desktop image only grew in v0.2.0, so an unspecified desktop tag must not
// track whatever the API happens to be tagged.
var apiTag = empty(apiImageTag) ? imageTag : apiImageTag
var workerTag = empty(workerImageTag) ? imageTag : workerImageTag
var botDesktopTag = empty(botDesktopImageTag) ? imageTag : botDesktopImageTag

@description('Deploy the api/worker Container Apps. Turn off for the very first deployment, when ACR is still empty and there is no image to pull.')
param deployApps bool = true

@description('Extra tags merged onto every resource.')
param tags object = {}

// --------------------------------------------------------------------------
// Variables
// --------------------------------------------------------------------------

var namePrefix = 'nesqbot-${environmentName}'

// The two axes. isProd decides how strict we are; isFull decides how much we
// spend. Nothing below may read isProd to pick a SKU, and nothing may read
// isFull to pick a security setting - conflating the two is what made
// "run production" and "run expensive" the same flag.
var isProd = environmentName == 'prod'
var isFull = sizingTier == 'full'

var commonTags = union(
  {
    application: 'nesqbot'
    environment: environmentName
    managedBy: 'bicep'
  },
  tags
)

// Storage account and Key Vault names: lowercase alphanumeric only, and 24
// characters is the hard cap for both.
var storageName = take('nesq${environmentName}st${replace(suffix, '-', '')}', 24)
var keyVaultName = take('nesq-${environmentName}-kv-${suffix}', 24)
var acrName = take('nesqacr${environmentName}${replace(suffix, '-', '')}', 50)

var postgresDatabaseName = 'nesqbot'
// Temporal keeps its own state, on the same server so there is one thing to
// back up, one firewall to reason about and one bill.
var temporalDatabaseName = 'temporal'
var temporalVisibilityDatabaseName = 'temporal_visibility'

// -------------------------------------------------------------------------
// SIZING - driven by sizingTier only.
// -------------------------------------------------------------------------
// Burstable B2s supports neither zone-redundant HA nor geo-redundant backup:
// both are GeneralPurpose-and-up features. They belong on the sizing axis
// rather than the prod axis precisely because the platform ties them to the
// SKU - asking for them on a lean tier is a deployment error, not a stricter
// posture.
var postgresSku = isFull ? { name: 'Standard_D2ds_v5', tier: 'GeneralPurpose' } : { name: 'Standard_B2s', tier: 'Burstable' }
var postgresStorageGb = isFull ? 128 : 32
var postgresHaMode = isFull ? 'ZoneRedundant' : 'Disabled'
var postgresGeoBackup = isFull ? 'Enabled' : 'Disabled'
// Redis, and why there are two completely different answers here.
//
// TWO products are retired here, not one. Microsoft.Cache/redis (Azure Cache
// for Redis) refuses new creations, and so does the Enterprise_E* /
// EnterpriseFlash_F* family on Microsoft.Cache/redisEnterprise:
//   "Creation of new Azure Cache for Redis Enterprise resources is no longer
//    supported ... please create Azure Managed Redis instead"
// Azure Managed Redis is the survivor. Confusingly it lives on the same
// redisEnterprise resource type; what makes it AMR rather than Enterprise is
// the SKU family - Balanced_B*, MemoryOptimized_M*, ComputeOptimized_X*.
//
// Do not trust Microsoft.Cache/skus here. For swedencentral it lists only the
// retiring Enterprise families and omits every AMR family, across all three
// api-versions tried. Balanced_B0 nevertheless passes preflight in this region
// (verified with az deployment group validate on 2026-08-22), so the SKU list
// is stale rather than authoritative. Preflight a candidate SKU before
// believing either.
//
// So: lean runs Redis as a Container App, the same pattern as Temporal. That
// is defensible here specifically because Redis is not a datastore in this
// system - app/services/events.py uses it for SSE pub/sub fanout and already
// falls back to an in-process asyncio.Queue. Nothing durable lives in it.
//
// It is still not optional. With more than one API replica, in-process fanout
// alone means a client on replica A never sees an event published on replica
// B, so dropping Redis would silently cap the API at one replica.
//
// The lean choice is a genuine trade, not a forced move: Balanced_B0 is a real
// managed option at roughly USD 55/month against roughly USD 6-20 for the
// container. It buys a managed control plane and TLS for a component that
// stores nothing. Revisit if Redis ever holds something worth keeping.
var redisAppName = '${namePrefix}-redis'
var redisEnterpriseName = '${namePrefix}-redis-ent'
// 0.25 vCPU / 0.5Gi is the Container Apps Consumption floor and is generous
// for pub/sub. maxmemory sits below the container limit so Redis evicts or
// refuses before the platform OOM-kills the process.
var redisContainerCpu = json('0.25')
var redisContainerMemory = '0.5Gi'
var redisContainerMaxMemory = '320mb'
// Plaintext inside the environment. TCP ingress does not terminate TLS, and
// the endpoint does not resolve outside the VNet.
//
// ADDRESSED BY BARE APP NAME, NOT BY THE .internal FQDN. This is the one
// place HTTP and TCP ingress genuinely differ, and it presents as a hang
// rather than an error. <app>.internal.<defaultDomain> resolves to the
// environment's shared HTTP edge proxy - every app in the environment
// resolves to the SAME address - and that proxy only speaks HTTP/HTTPS, so a
// RESP connection to it is accepted by nothing and stalls until the client
// times out. Verified from inside the API container: the FQDN resolved to the
// shared proxy IP and timed out, while 'nesqbot-redis' resolved to the
// app's own service IP and answered PING with +PONG. For TCP ingress the
// reachable form is <appName>:<exposedPort> - and exposedPort, not just
// targetPort, is what makes that address exist at all.
var redisInternalEndpoint = 'redis://${redisAppName}:6379/0'
var storageSkuName = isFull ? 'Standard_ZRS' : 'Standard_LRS'
var acrSkuName = isFull ? 'Standard' : 'Basic'
var botHomesQuotaGb = isFull ? 1024 : 100

// Container Apps Consumption bills per vCPU-second, so every replica floor is
// a standing charge. Consumption also only accepts 2 GiB of memory per vCPU.
var apiMinReplicas = isFull ? 2 : 1
var apiMaxReplicas = isFull ? 10 : 3
var workerMinReplicas = 1
var workerMaxReplicas = isFull ? 5 : 2
var apiCpu = isFull ? json('1.0') : json('0.5')
var apiMemory = isFull ? '2Gi' : '1Gi'
var workerCpu = isFull ? json('1.0') : json('0.5')
var workerMemory = isFull ? '2Gi' : '1Gi'
// Temporal auto-setup runs frontend, history, matching and worker in one
// process. 0.5 vCPU / 1Gi is the floor that still starts reliably at 128
// shards; if the history service starts OOM-looping, this is the knob.
var temporalCpu = isFull ? json('1.0') : json('0.5')
var temporalMemory = isFull ? '2Gi' : '1Gi'

// -------------------------------------------------------------------------
// HARDENING - driven by environmentName only. None of this costs real money.
// -------------------------------------------------------------------------
var logRetentionDays = isProd ? 90 : 30
var postgresBackupDays = isProd ? 35 : 7
var blobRetentionDays = isProd ? 30 : 7
var kvSoftDeleteDays = isProd ? 90 : 7
// Localhost has no business in a production allow-list. apps/api reads
// CORS_ORIGINS for its own check; the ingress policy below mirrors it, so both
// layers agree instead of one quietly being wider than the other.
var defaultAllowedOrigins = isProd
  ? [
      'https://app.nesqualtech.com'
      'https://mobile.nesqualtech.com'
    ]
  : [
      'https://app.nesqualtech.com'
      'http://localhost:1420'
      'http://localhost:8081'
    ]
var allowedOrigins = empty(apiAllowedOrigins) ? defaultAllowedOrigins : apiAllowedOrigins

// -------------------------------------------------------------------------
// Temporal endpoint
// -------------------------------------------------------------------------
var temporalAppName = '${namePrefix}-temporal'
// Built from the environment's default domain rather than from the Temporal
// app's own fqdn on purpose: referencing that resource would make the API and
// worker wait on Temporal at deploy time, and both are written to treat an
// unreachable Temporal as degraded rather than fatal.
// Bare app name for the same reason as redisInternalEndpoint above: this is
// TCP ingress, so the .internal.<defaultDomain> form points at the shared
// HTTP proxy and a gRPC dial to it never connects.
var temporalInternalEndpoint = '${temporalAppName}:7233'
var temporalEndpoint = !empty(temporalHost) ? temporalHost : (deployTemporal ? temporalInternalEndpoint : '')

// ARM's if() only evaluates the branch it returns, so listKeys() here is never
// called on the lean tier where the Enterprise resources do not exist.
#disable-next-line BCP318 BCP422 // both sides guarded by isFull
var redisUrl = isFull ? 'rediss://:${uriComponent(redisEnterpriseDb.listKeys().primaryKey)}@${redisEnterprise.properties.hostName}:10000/0' : redisInternalEndpoint

// Built-in role definition ids.
var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var roleKeyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleStorageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var roleStorageFileDataSmbShareContributor = '0c867c2a-1d8c-454a-a3db-ab2ea1bdc8bb'
var roleCognitiveServicesOpenAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var roleMonitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'
// "Azure Container Instances Contributor Role" - Microsoft.ContainerInstance/
// containerGroups/* and nothing that can touch another resource type.
var roleAciContributor = '5d977122-f97e-4b4d-a52f-6b43003ddb4d'
// "Managed Identity Operator" - the only role that lets a caller attach a
// user-assigned identity to a resource it creates. Scoped to the one identity.
var roleManagedIdentityOperator = 'f1a07417-d97a-45cb-824c-7a7467783830'
// Custom subnet-join role, defined below. A custom role definition is a
// subscription-level object whatever its assignableScopes say, so the
// assignment has to reference the subscription-scoped id - `role.id` inside a
// resource-group deployment compiles to a resourceGroup()-scoped resourceId
// that ARM will not resolve.
var roleAciSubnetJoinName = guid(resourceGroup().id, 'nesq-aci-subnet-join')

// --------------------------------------------------------------------------
// Identity
// --------------------------------------------------------------------------

@description('One user-assigned identity shared by the API and worker. Simpler to reason about than two, and both need the same four grants.')
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id'
  location: location
  tags: commonTags
}

// --------------------------------------------------------------------------
// Networking
// --------------------------------------------------------------------------
// One VNet carries both halves of the desktop story: the Container Apps
// environment sits on an infrastructure subnet, and Bot Desktops are injected
// into a subnet delegated to Microsoft.ContainerInstance. Same VNet means the
// API reaches a desktop on its private IP by plain routing - no public
// endpoint, no NAT, nothing to expose.

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: '${namePrefix}-vnet'
  location: location
  tags: commonTags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
  }
}

// No NSG on this one. Container Apps needs a long list of inbound and outbound
// flows on its infrastructure subnet and Microsoft reserves the right to add
// more; an NSG here is a future outage with a slow diagnosis.
resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-01-01' = {
  parent: vnet
  name: 'snet-container-apps'
  properties: {
    addressPrefix: containerAppsSubnetPrefix
    delegations: [
      {
        name: 'container-apps'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
    privateEndpointNetworkPolicies: 'Disabled'
  }
}

// Outbound egress for the desktop subnet.
//
// This is not optional and it is the failure nobody sees coming. Azure retired
// default outbound access for new deployments on 2025-09-30: a freshly created
// subnet has no implicit SNAT to the internet, and a container group cannot
// carry a public IP of its own once it is VNet-injected. Without an explicit
// egress path the ACR pull does not fail fast - it hangs until the container
// group start timeout, and the only symptom is a desktop that never becomes
// ready.
//
// A NAT gateway rather than a private endpoint for ACR, for two reasons: an
// ACR private endpoint needs the Premium SKU, which costs more per month than
// the gateway; and a Bot Desktop is a browser whose entire job is reaching
// third-party SaaS, so it needs general internet egress regardless. The NSG
// below is what narrows that back down.
resource aciNatPublicIp 'Microsoft.Network/publicIPAddresses@2024-01-01' = if (deployAciNatGateway) {
  name: '${namePrefix}-aci-nat-pip'
  location: location
  tags: commonTags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    idleTimeoutInMinutes: 10
  }
}

resource aciNat 'Microsoft.Network/natGateways@2024-01-01' = if (deployAciNatGateway) {
  name: '${namePrefix}-aci-nat'
  location: location
  tags: commonTags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        #disable-next-line BCP318 // guarded by the same condition
        id: aciNatPublicIp.id
      }
    ]
  }
}

// A desktop is a browser doing whatever a prompt talked it into, so the subnet
// is treated as hostile in both directions. This mirrors the NetworkPolicy in
// infra/bot-desktop/k8s/desktop-template.yaml, which the AKS path already had:
//
//   inbound   6901 (noVNC) and 7910 (sidecar) from the Container Apps subnet,
//             nothing else from the VNet
//   outbound  53, 80 and 443 to the internet, nothing else, and explicitly not
//             to RFC1918 or link-local
//
// The outbound denies come first on purpose. RFC1918 is not inside the
// Internet service tag so the allow rules would not have matched it anyway,
// but a peering added later could route it, and a rule that only works by
// accident is a rule that stops working.
//
// Per-bot isolation is the product's differentiator. It should hold at the
// network layer and not only in the orchestration story.
resource aciNsg 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: '${namePrefix}-aci-nsg'
  location: location
  tags: commonTags
  properties: {
    securityRules: [
      {
        name: 'AllowContainerAppsToDesktopPorts'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: containerAppsSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: aciSubnetPrefix
          destinationPortRanges: [
            '6901' // noVNC stream
            '7910' // sidecar control plane
          ]
        }
      }
      {
        // Overrides the default AllowVnetInBound. If desktops ever go
        // unreachable after a topology change, look here first.
        name: 'DenyEverythingElseFromVnet'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
      {
        // Link-local, which is IMDS. A desktop has no managed identity to
        // fetch a token for; the image pull identity is attached by the ACI
        // control plane, not requested from inside the container.
        name: 'DenyLinkLocalOutbound'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '169.254.0.0/16'
          destinationPortRange: '*'
        }
      }
      {
        // Covers this VNet as well - 10.60.0.0/16 sits inside 10.0.0.0/8 - so
        // a prompt-injected page cannot reach Postgres, Redis, the API's
        // internal ingress, or another bot's desktop.
        name: 'DenyRfc1918Outbound'
        properties: {
          priority: 110
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefixes: [
            '10.0.0.0/8'
            '172.16.0.0/12'
            '192.168.0.0/16'
          ]
          destinationPortRange: '*'
        }
      }
      {
        // Azure's platform DNS resolver. Not covered by the Internet service
        // tag, and the desktop resolves nothing without it.
        name: 'AllowAzureDnsOutbound'
        properties: {
          priority: 200
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '168.63.129.16'
          destinationPortRange: '53'
        }
      }
      {
        // Browsing, and the ACR pull, which is 443 to a public endpoint.
        name: 'AllowWebOutbound'
        properties: {
          priority: 210
          direction: 'Outbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRanges: [
            '80'
            '443'
          ]
        }
      }
      {
        name: 'AllowPublicDnsOutbound'
        properties: {
          priority: 220
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRange: '53'
        }
      }
      {
        // Overrides the default AllowInternetOutBound, so a compromised
        // desktop cannot exfiltrate over an arbitrary port. Second place to
        // look when a desktop behaves oddly, after the inbound deny above.
        name: 'DenyEverythingElseOutbound'
        properties: {
          priority: 4000
          direction: 'Outbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

// Delegation to Microsoft.ContainerInstance/containerGroups is what puts a
// container group on a private IP in this subnet. Without it ACI rejects the
// subnetIds argument outright.
resource aciSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-01-01' = {
  parent: vnet
  name: 'snet-bot-desktops'
  properties: {
    // /23 is 507 usable addresses, one per running container group. The real
    // ceiling is the subscription's ACI core quota, not IP space.
    addressPrefix: aciSubnetPrefix
    // Pinned rather than left to the API default, which differs by api-version
    // and would otherwise show up as a spurious change on every what-if.
    // Enabled is what makes the NSG above apply to a private endpoint NIC.
    privateEndpointNetworkPolicies: 'Enabled'
    networkSecurityGroup: {
      id: aciNsg.id
    }
    natGateway: deployAciNatGateway
      ? {
          #disable-next-line BCP318 // guarded by the ternary
          id: aciNat.id
        }
      : null
    delegations: [
      {
        name: 'container-instance'
        properties: {
          serviceName: 'Microsoft.ContainerInstance/containerGroups'
        }
      }
    ]
  }
  // Subnet writes on one VNet are a serialised control-plane operation.
  dependsOn: [
    containerAppsSubnet
  ]
}

// --------------------------------------------------------------------------
// Observability
// --------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  tags: commonTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-ai'
  location: location
  tags: commonTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// --------------------------------------------------------------------------
// Key Vault
// --------------------------------------------------------------------------

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: commonTags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enabledForDeployment: false
    enabledForTemplateDeployment: false
    enabledForDiskEncryption: false
    enableSoftDelete: true
    softDeleteRetentionInDays: kvSoftDeleteDays
    // Purge protection is irreversible once on, so prod only - a dev vault
    // that cannot be purged blocks redeploying under the same name.
    enablePurgeProtection: isProd ? true : null
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

// Seed the vault from the secure params so the apps have one place to read
// from and rotation is a vault operation, not a redeploy.
resource kvJwtSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'jwt-secret'
  properties: {
    value: jwtSecret
    contentType: 'text/plain'
  }
}

resource kvWorkerToken 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'worker-api-token'
  properties: {
    value: workerApiToken
    contentType: 'text/plain'
  }
}

resource kvSidecarToken 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'sidecar-token'
  properties: {
    value: sidecarToken
    contentType: 'text/plain'
  }
}

resource kvPostgresPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'postgres-admin-password'
  properties: {
    value: postgresAdminPassword
    contentType: 'text/plain'
  }
}

resource kvDatabaseUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'database-url'
  properties: {
    value: 'postgresql+asyncpg://${postgresAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres.properties.fullyQualifiedDomainName}:5432/${postgresDatabaseName}?ssl=require'
    contentType: 'text/plain'
  }
}

// ACI mounts Azure Files with the account key - it has no identity-based
// option for SMB volumes, unlike blob. The key therefore has to exist
// somewhere the desktop driver can read it, and Key Vault is that place: the
// API's identity already holds Key Vault Secrets User, so nothing new is
// granted and rotation stays a vault operation.
resource kvStorageAccountKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'storage-account-key'
  properties: {
    value: storage.listKeys().keys[0].value
    contentType: 'text/plain'
  }
}

// Repointed, not removed. On the lean tier this holds a plain
// redis://<appName>:6379/0 with no credential in it - it is not a secret
// so much as one more place the apps and an operator can agree on the
// endpoint. On the full tier it carries the Enterprise access key and is a
// secret in earnest. Same name either way, so nothing downstream has to care.
resource kvRedisUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'redis-url'
  properties: {
    value: redisUrl
    contentType: 'text/plain'
  }
}

// --------------------------------------------------------------------------
// Storage: Blob (artifacts, screenshots) + Files (bot homes)
// --------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: commonTags
  sku: {
    name: storageSkuName
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true // Azure Files SMB mounts still need the key
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: blobRetentionDays
    }
  }
}

resource artifactsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'artifacts'
  properties: {
    publicAccess: 'None'
  }
}

resource screenshotsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'screenshots'
  properties: {
    publicAccess: 'None'
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    // Declared explicitly because this resource is PUT whole: leaving
    // properties off resets share soft-delete to the service default instead
    // of preserving whatever is there. Bot homes hold logged-in browser
    // profiles, so an accidental share delete should be recoverable.
    shareDeleteRetentionPolicy: {
      enabled: true
      days: blobRetentionDays
    }
  }
}

resource botHomesShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'bot-homes'
  properties: {
    shareQuota: botHomesQuotaGb
    enabledProtocols: 'SMB'
  }
}

// --------------------------------------------------------------------------
// Container registry
// --------------------------------------------------------------------------

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: commonTags
  sku: {
    name: acrSkuName
  }
  properties: {
    // Admin credentials are a shared static password. Container Apps and AKS
    // both pull with the managed identity instead.
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// --------------------------------------------------------------------------
// PostgreSQL Flexible Server (+ pgvector)
// --------------------------------------------------------------------------

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${namePrefix}-pg'
  location: location
  tags: commonTags
  sku: postgresSku
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: postgresStorageGb
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: postgresBackupDays
      geoRedundantBackup: postgresGeoBackup
    }
    highAvailability: {
      mode: postgresHaMode
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Enabled'
      tenantId: subscription().tenantId
    }
  }
}

// pgvector has to be allow-listed at the server level BEFORE
// `CREATE EXTENSION vector` can succeed inside the database. This is the piece
// people miss: the extension ships with the server but is refused until it
// appears in azure.extensions.
resource postgresExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    // BTREE_GIN is not optional when deployTemporal is true: temporal's
    // visibility schema v1.2 runs `CREATE EXTENSION IF NOT EXISTS btree_gin`,
    // Azure refuses it unless allow-listed here, and auto-setup then dies with
    // ActivationFailed - the whole Temporal app never starts and every routine
    // silently falls back to inline execution. BTREE_GIST rides along because
    // later temporal schema versions reach for it too.
    value: 'VECTOR,PGCRYPTO,PG_STAT_STATEMENTS,UUID-OSSP,BTREE_GIN,BTREE_GIST'
    source: 'user-override'
  }
}

// Must be serialised after the configuration change: Postgres Flexible Server
// rejects concurrent control-plane operations on the same server.
resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: postgresDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    postgresExtensions
  ]
}

// Temporal keeps history and visibility in two databases. ARM creates them so
// the schema tool never needs CREATEDB; temporalio/auto-setup then runs
// setup-schema / update-schema inside them, which is idempotent across
// restarts. SKIP_DB_CREATE=true on the Temporal app is the other half of this.
resource temporalDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (deployTemporal) {
  parent: postgres
  name: temporalDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    postgresDatabase
  ]
}

resource temporalVisibilityDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (deployTemporal) {
  parent: postgres
  name: temporalVisibilityDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    temporalDatabase
  ]
}

// Container Apps egress IPs are not stable, so the app tier reaches Postgres
// through the "allow Azure services" pseudo-rule. Replace with a private
// endpoint + VNet-integrated Container Apps environment before this holds
// anything regulated - see the placeholder note in infra/azure/README.md.
resource postgresAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAllAzureServicesAndResources'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
  dependsOn: [
    postgresDatabase
    temporalDatabase
    temporalVisibilityDatabase
  ]
}

// --------------------------------------------------------------------------
// Redis
// --------------------------------------------------------------------------

// LEAN: Redis as a Container App, internal TCP ingress on 6379.
//
// NO PERSISTENCE, DELIBERATELY. There is no volume, RDB snapshotting is off
// and AOF is off, so a restart loses everything in it. That is correct for SSE
// pub/sub fanout and it is the whole reason this is allowed to be a container
// rather than a managed service. Do not put a queue, a session store, a lock,
// or a cache anything depends on surviving in here without revisiting this
// decision first - by the time you notice, the restart will already have
// happened.
resource redisApp 'Microsoft.App/containerApps@2024-03-01' = if (!isFull) {
  name: redisAppName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        // No authentication on the wire, so external must stay false. Reachable
        // only from inside the Container Apps environment; the ACI desktop
        // subnet is denied outbound to RFC1918 and cannot reach it either.
        external: false
        transport: 'tcp'
        targetPort: 6379
        exposedPort: 6379
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      // Present so pointing redisImage at an ACR mirror needs no other change
      // - two public Docker Hub images is two anonymous pull limits to hit.
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'redis'
          image: redisImage
          // Overrides CMD and keeps the image's entrypoint, exactly as the
          // command: block in docker-compose.yml does.
          args: [
            'redis-server'
            // Empty string disables RDB snapshotting outright. Compose keeps
            // "--save 60 1" because it has a volume to write to; this has none.
            '--save'
            ''
            '--appendonly'
            'no'
            '--maxmemory'
            redisContainerMaxMemory
            // The event bus must not silently drop keys.
            '--maxmemory-policy'
            'noeviction'
            // No bind directive and no password would otherwise put Redis in
            // protected mode, where it refuses every non-loopback connection -
            // including the ingress proxy. This is safe only because ingress
            // is internal.
            '--protected-mode'
            'no'
          ]
          resources: {
            cpu: redisContainerCpu
            memory: redisContainerMemory
          }
          probes: [
            {
              type: 'Readiness'
              tcpSocket: {
                port: 6379
              }
              periodSeconds: 10
              failureThreshold: 6
            }
            {
              type: 'Liveness'
              tcpSocket: {
                port: 6379
              }
              initialDelaySeconds: 15
              periodSeconds: 30
              failureThreshold: 5
            }
          ]
        }
      ]
      // Exactly one. Two replicas would be two unrelated Redis processes
      // behind one name, and a subscriber on one would never see a publish on
      // the other - the precise bug this component exists to prevent.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    acrPull
  ]
}

// FULL: Azure Managed Redis, because a single container is not an HA story.
//
// Balanced_B1 (1 GB) with highAvailability Enabled: replicated and zone
// redundant, which the container above is not. Roughly USD 90/month.
//
// NOT Enterprise_E1, which an earlier draft of this template used and which
// fails preflight outright - that family is retiring alongside Azure Cache for
// Redis. AMR B-series is the replacement. Verified by preflighting each
// candidate rather than by reading the SKUs list, which is stale for this
// region; see the note on the variables above.
resource redisEnterprise 'Microsoft.Cache/redisEnterprise@2025-07-01' = if (isFull) {
  name: redisEnterpriseName
  location: location
  tags: commonTags
  sku: {
    // No capacity: AMR sizes come from the SKU name, unlike the old Enterprise
    // family where capacity was a separate multiplier.
    name: 'Balanced_B1'
  }
  properties: {
    minimumTlsVersion: '1.2'
    highAvailability: 'Enabled'
    // Required from api-version 2025-07-01 - omitting it fails preflight with
    // "'properties.publicNetworkAccess' is required", not a default.
    // Same posture as every other data service here: reachable from the
    // Container Apps environment, whose egress IPs are not stable. Swap for a
    // private endpoint in the same pass that does Postgres and Storage.
    publicNetworkAccess: 'Enabled'
  }
}

resource redisEnterpriseDb 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = if (isFull) {
  parent: redisEnterprise
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    evictionPolicy: 'NoEviction'
    // EnterpriseCluster gives one endpoint behind a proxy, which is what a
    // non-cluster-aware client like redis-py expects.
    clusteringPolicy: 'EnterpriseCluster'
  }
}

// --------------------------------------------------------------------------
// Azure OpenAI / AI Foundry
// --------------------------------------------------------------------------

resource openai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: '${namePrefix}-aoai'
  location: location
  tags: commonTags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: '${namePrefix}-aoai-${suffix}'
    publicNetworkAccess: 'Enabled'
    // Forces Entra tokens; the managed identity holds "Cognitive Services
    // OpenAI User", so no API key ever has to exist.
    disableLocalAuth: false
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

// Deployments must be serialised - the control plane rejects parallel writes
// to the same account - hence the dependsOn chain rather than four independent
// resources.
// Deployment names are contracts: apps/api/app/config.py defaults to exactly
// these strings, and services/model_router.py prices them. Renaming one here
// without renaming it there routes every call to a 404.
//
// Verified present in swedencentral on 2026-08-22 with
//   az cognitiveservices model list -l swedencentral
// Every one of the four is GlobalStandard - note that text-embedding-3-small
// is NOT offered as plain 'Standard' in this region, only GlobalStandard and
// DataZoneStandard, which is what the previous sku name here would have hit.
//
// Capacity is in thousands of tokens per minute and is a rate limit, not a
// reservation: GlobalStandard bills per token, so a modest number costs
// nothing extra and simply throttles a runaway loop earlier.
resource deployNano 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: 'gpt-5.6-luna'
  sku: {
    name: 'GlobalStandard'
    // Capacity is thousands of tokens/minute, and the agent loop makes this a
    // correctness setting, not a tuning knob. sol shipped at 10 (=10k TPM);
    // one vision step carries a screenshot, so the loop exhausted a minute's
    // budget in a couple of steps, took 429s, and backed off - which is what a
    // user experienced as "five minutes to open LinkedIn". Subscription quota
    // is 2000 per model and almost none of it was in use.
    capacity: isFull ? 400 : 200
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-luna'
      version: '2026-07-09'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

resource deployMini 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: 'gpt-5.4-mini'
  sku: {
    name: 'GlobalStandard'
    capacity: isFull ? 400 : 250
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-mini'
      version: '2026-03-17'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    deployNano
  ]
}

resource deployReason 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: 'gpt-5.6-sol'
  sku: {
    name: 'GlobalStandard'
    // The agent loop's model - the one that was starved.
    capacity: isFull ? 800 : 500
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-sol'
      version: '2026-07-09'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    deployMini
  ]
}

resource deployEmbed 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: 'text-embedding-3-small'
  sku: {
    name: 'GlobalStandard'
    capacity: isFull ? 120 : 20
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
    // Stated explicitly like the other three. Content filtering is moot for an
    // embedding model, but leaving it unset means the service default shows as
    // a drifting property forever.
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    deployReason
  ]
}

// --------------------------------------------------------------------------
// RBAC - least privilege for the shared managed identity
// --------------------------------------------------------------------------

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, roleAcrPull)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
  }
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, uami.id, roleKeyVaultSecretsUser)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUser)
  }
}

resource blobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, uami.id, roleStorageBlobDataContributor)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataContributor)
  }
}

resource fileContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, uami.id, roleStorageFileDataSmbShareContributor)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageFileDataSmbShareContributor)
  }
}

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openai
  name: guid(openai.id, uami.id, roleCognitiveServicesOpenAiUser)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleCognitiveServicesOpenAiUser)
  }
}

resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, uami.id, roleMonitoringMetricsPublisher)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleMonitoringMetricsPublisher)
  }
}

// --------------------------------------------------------------------------
// RBAC - Bot Desktops on Azure Container Instances
// --------------------------------------------------------------------------
// Spawning a desktop is three separate authorisation decisions, and only the
// first one is obvious. Granting Contributor on the resource group would cover
// all three and also let a prompt-injected desktop driver drop the Postgres
// server, so instead:
//
//   1. containerGroups CRUD      -> "Azure Container Instances Contributor
//                                    Role", the narrowest built-in that exists
//                                    for this. Its action set is
//                                    Microsoft.ContainerInstance/
//                                    containerGroups/* plus reads; it cannot
//                                    see, let alone touch, any other provider.
//                                    Scoped to the resource group because the
//                                    container groups do not exist yet.
//   2. join the delegated subnet -> a custom role with exactly
//                                    subnets/read + subnets/join/action,
//                                    scoped to the one subnet. The built-in
//                                    alternative, Network Contributor, also
//                                    carries subnets/delete.
//   3. attach the identity       -> "Managed Identity Operator", scoped to
//                                    this identity alone. Without it the
//                                    container group cannot carry a UAMI, and
//                                    without a UAMI the only way to pull the
//                                    desktop image is ACR admin credentials.

resource aciContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: resourceGroup()
  name: guid(resourceGroup().id, uami.id, roleAciContributor)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAciContributor)
  }
}

// Custom, because no built-in grants subnet join without also granting subnet
// destruction. assignableScopes is this resource group only.
resource aciSubnetJoinRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: roleAciSubnetJoinName
  properties: {
    roleName: 'Nesq Bot Desktop Subnet Join (${namePrefix})'
    description: 'Join Bot Desktop container groups to the delegated ACI subnet. Read and join only - no write, no delete.'
    type: 'CustomRole'
    assignableScopes: [
      resourceGroup().id
    ]
    permissions: [
      {
        actions: [
          'Microsoft.Network/virtualNetworks/subnets/read'
          'Microsoft.Network/virtualNetworks/subnets/join/action'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
}

resource aciSubnetJoin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aciSubnet
  name: guid(aciSubnet.id, uami.id, roleAciSubnetJoinName)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAciSubnetJoinName)
  }
  // subscriptionResourceId() is a string, so the dependency has to be explicit.
  dependsOn: [
    aciSubnetJoinRole
  ]
}

resource aciIdentityOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: uami
  name: guid(uami.id, uami.id, roleManagedIdentityOperator)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleManagedIdentityOperator)
  }
}

// --------------------------------------------------------------------------
// Container Apps
// --------------------------------------------------------------------------

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-cae'
  location: location
  tags: commonTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
    // internal: false keeps the API's public ingress. The VNet is here so the
    // environment's egress lands on a subnet that routes privately to the ACI
    // desktop subnet next door - not to make the environment private.
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsSubnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// Azure Files mount for the bot homes, so the API can write a bot's home
// directory even in aks/Container Apps mode.
resource caeStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: cae
  name: 'bot-homes'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: botHomesShare.name
      accessMode: 'ReadWrite'
    }
  }
}

var sharedEnv = [
  {
    name: 'NESQ_ENV'
    value: environmentName == 'prod' ? 'production' : environmentName
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: openai.properties.endpoint
  }
  {
    name: 'AZURE_OPENAI_API_VERSION'
    value: '2024-12-01-preview'
  }
  {
    name: 'AZURE_DEPLOYMENT_NANO'
    value: deployNano.name
  }
  {
    name: 'AZURE_DEPLOYMENT_MINI'
    value: deployMini.name
  }
  {
    name: 'AZURE_DEPLOYMENT_REASON'
    value: deployReason.name
  }
  {
    name: 'AZURE_DEPLOYMENT_EMBED'
    value: deployEmbed.name
  }
  {
    name: 'AZURE_KEY_VAULT_URL'
    value: kv.properties.vaultUri
  }
  {
    name: 'AZURE_TENANT_ID'
    value: entraTenantId
  }
  {
    name: 'AZURE_CLIENT_ID'
    // The API's OWN app id - the audience it will accept on an access token.
    // NOT the desktop/mobile client id: both are issued by the same tenant and
    // verify against the same JWKS, so the client's id here would make the API
    // silently accept tokens minted for a different audience.
    value: empty(entraApiAppId) ? uami.properties.clientId : entraApiAppId
  }
  {
    name: 'AZURE_API_SCOPE'
    // The API returns 503 rather than 401 when this is blank, so a missing
    // value cannot quietly degrade into "any scope will do".
    value: entraApiScope
  }
  {
    name: 'AZURE_MANAGED_IDENTITY_CLIENT_ID'
    // Always the user-assigned identity, unlike AZURE_CLIENT_ID above, which
    // becomes the Entra app id once sign-in is wired up. Anything doing ARM
    // calls - the ACI desktop driver in particular - must authenticate as the
    // UAMI, so it needs a name that entraClientId can never take over.
    value: uami.properties.clientId
  }
  {
    name: 'TEMPORAL_HOST'
    value: temporalEndpoint
  }
  {
    name: 'TEMPORAL_NAMESPACE'
    value: temporalNamespace
  }
  {
    name: 'TEMPORAL_TASK_QUEUE'
    value: 'nesq-bot'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsights.properties.ConnectionString
  }
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'REDIS_URL'
    secretRef: 'redis-url'
  }
  {
    name: 'JWT_SECRET'
    secretRef: 'jwt-secret'
  }
]

// Secrets are set from the secure params rather than as Key Vault references.
// A keyVaultUrl secretRef is resolved at app-create time, which would fail on
// a first deploy where the vault entries do not exist yet - a chicken-and-egg
// that makes the whole template undeployable from scratch. The same values are
// written to Key Vault above, so rotation has a single home.
var sharedSecrets = [
  {
    name: 'database-url'
    value: 'postgresql+asyncpg://${postgresAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres.properties.fullyQualifiedDomainName}:5432/${postgresDatabaseName}?ssl=require'
  }
  {
    name: 'redis-url'
    value: redisUrl
  }
  {
    name: 'jwt-secret'
    value: jwtSecret
  }
  {
    name: 'worker-api-token'
    value: workerApiToken
  }
  {
    name: 'sidecar-token'
    value: sidecarToken
  }
]

resource apiApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${namePrefix}-api'
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        corsPolicy: {
          allowedOrigins: allowedOrigins
          allowedMethods: ['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS']
          allowedHeaders: ['*']
          allowCredentials: true
        }
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: '${acr.properties.loginServer}/nesqbot/api:${apiTag}'
          env: concat(sharedEnv, [
            {
              // Ingress CORS above rejects a bad Origin at the edge; the app
              // checks it again. Both read the same list.
              name: 'CORS_ORIGINS'
              value: join(allowedOrigins, ',')
            }
            {
              name: 'BOT_DESKTOP_MODE'
              value: botDesktopMode
            }
            {
              name: 'BOT_DESKTOP_IMAGE'
              value: '${acr.properties.loginServer}/nesqbot/bot-desktop:${botDesktopTag}'
            }
            {
              name: 'BOT_DESKTOP_HOME_ROOT'
              value: '/mnt/bot-homes'
            }
            {
              name: 'BOTS_DIR'
              value: '/bots'
            }
            // Bot Desktops on ACI. One container group per bot, injected into
            // the delegated subnet so it gets a private IP and no public
            // endpoint, pulling from ACR with the user-assigned identity.
            {
              name: 'ACI_RESOURCE_GROUP'
              value: resourceGroup().name
            }
            {
              name: 'ACI_SUBSCRIPTION_ID'
              value: subscription().subscriptionId
            }
            {
              name: 'ACI_REGION'
              value: location
            }
            {
              name: 'ACI_SUBNET_ID'
              value: aciSubnet.id
            }
            {
              name: 'ACI_CPU'
              value: string(aciCpu)
            }
            {
              name: 'ACI_MEMORY_GB'
              value: string(aciMemoryGb)
            }
            {
              name: 'ACI_REGISTRY_SERVER'
              value: acr.properties.loginServer
            }
            {
              // Full ARM resource id, not the client id: the ACI SDK keys both
              // userAssignedIdentities and ImageRegistryCredential.identity by
              // resource id, and a client id there fails at create time.
              name: 'ACI_REGISTRY_IDENTITY'
              value: uami.id
            }
            // Azure Files for bot homes. Without a volume every stop destroys
            // the bot's home directory, browser profile and logged-in sessions
            // with it - which would undo the one thing a Bot Desktop is for.
            // Same share the API mounts at /mnt/bot-homes, so what the API
            // writes into a bot's home is what the desktop sees.
            {
              name: 'ACI_FILES_ACCOUNT'
              value: storage.name
            }
            {
              name: 'ACI_FILES_SHARE'
              value: botHomesShare.name
            }
            {
              // The key itself lives in Key Vault, not in this env block.
              // Read it with the same identity, from AZURE_KEY_VAULT_URL.
              name: 'ACI_FILES_KEY_SECRET_NAME'
              value: kvStorageAccountKey.name
            }
            {
              name: 'NESQ_SIDECAR_TOKEN'
              secretRef: 'sidecar-token'
            }
            {
              name: 'WORKER_API_TOKEN'
              secretRef: 'worker-api-token'
            }
          ])
          resources: {
            cpu: apiCpu
            memory: apiMemory
          }
          volumeMounts: [
            {
              volumeName: 'bot-homes'
              mountPath: '/mnt/bot-homes'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/health'
                port: 8080
              }
              initialDelaySeconds: 20
              periodSeconds: 30
              // Explicit for the same reason as readiness below: the default
              // is 1s, and a restart loop caused by a momentarily slow health
              // endpoint is far worse than a slightly late restart.
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                // Shallow, deliberately. `/api/health/deep` dials Postgres,
                // Redis and Temporal; it cannot answer inside the probe's
                // 1-second default timeout, so every replica stayed NotReady
                // and ingress had no backend - TCP connected to the load
                // balancer and then hung. Readiness answers "can this replica
                // serve traffic", and it can: the API degrades gracefully
                // without Redis or Temporal. Gating ingress on a dependency
                // sweep also means one slow dependency removes the whole app
                // from rotation instead of degrading it. Keep the deep check
                // for monitoring, not for routing.
                path: '/api/health'
                port: 8080
              }
              initialDelaySeconds: 10
              periodSeconds: 15
              timeoutSeconds: 5
              failureThreshold: 5
            }
            {
              type: 'Startup'
              httpGet: {
                path: '/api/health'
                port: 8080
              }
              periodSeconds: 5
              timeoutSeconds: 5
              failureThreshold: 24
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'bot-homes'
          storageType: 'AzureFile'
          storageName: caeStorage.name
        }
      ]
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: apiMaxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPull
    kvSecretsUser
    aciContributor
    aciSubnetJoin
    aciIdentityOperator
    // Creation order only - it does not wait for Redis to be serving. The
    // fanout in app/services/events.py degrades to in-process either way.
    redisApp
  ]
}

resource workerApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${namePrefix}-worker'
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      // No ingress: the worker only makes outbound calls.
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      secrets: sharedSecrets
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: '${acr.properties.loginServer}/nesqbot/worker:${workerTag}'
          env: concat(sharedEnv, [
            {
              name: 'API_INTERNAL_URL'
              #disable-next-line BCP318 // guarded by the ternary above
              value: deployApps ? 'https://${apiApp.properties.configuration.ingress.fqdn}' : ''
            }
            {
              name: 'API_DEV_HEADER'
              value: 'false'
            }
            {
              name: 'WORKER_API_TOKEN'
              secretRef: 'worker-api-token'
            }
            {
              name: 'NESQ_SIDECAR_TOKEN'
              secretRef: 'sidecar-token'
            }
          ])
          resources: {
            cpu: workerCpu
            memory: workerMemory
          }
        }
      ]
      scale: {
        minReplicas: workerMinReplicas
        maxReplicas: workerMaxReplicas
      }
    }
  }
  dependsOn: [
    acrPull
    kvSecretsUser
    redisApp
  ]
}

// --------------------------------------------------------------------------
// Temporal - self-hosted, internal only
// --------------------------------------------------------------------------
// Runs the same pinned image as docker-compose.yml against two databases on
// the Postgres server above, so workflow history never leaves the tenant or
// the region. Ingress is internal TCP: there is no public route to 7233 and
// no hostname outside the VNet that resolves to it.
//
// Deliberately not gated on deployApps. It pulls a public image, needs nothing
// from ACR, and the first deploy is the right time to run schema setup.
resource temporalApp 'Microsoft.App/containerApps@2024-03-01' = if (deployTemporal) {
  name: temporalAppName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        // external: false is the whole point. gRPC on 7233 is not something
        // that should ever answer from the internet.
        external: false
        transport: 'tcp'
        targetPort: 7233
        exposedPort: 7233
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      // Only the database password. Temporal has no business holding the JWT
      // signing key or the worker token.
      secrets: [
        {
          name: 'postgres-admin-password'
          value: postgresAdminPassword
        }
      ]
      // Present so that swapping temporalImage for an ACR copy - see the
      // az acr import note in infra/azure/README.md - needs no other change.
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'temporal'
          image: temporalImage
          args: [
            'autosetup'
          ]
          env: [
            {
              name: 'DB'
              value: 'postgres12'
            }
            {
              name: 'DB_PORT'
              value: '5432'
            }
            {
              name: 'POSTGRES_SEEDS'
              value: postgres.properties.fullyQualifiedDomainName
            }
            {
              name: 'POSTGRES_USER'
              value: postgresAdminLogin
            }
            {
              name: 'POSTGRES_PWD'
              secretRef: 'postgres-admin-password'
            }
            {
              name: 'POSTGRES_TLS_ENABLED'
              value: 'true'
            }
            {
              // Azure presents a certificate for the server FQDN and the
              // image trusts the system root store, so verification stays on.
              // If temporal-sql-tool ever fails the handshake, flipping this
              // to true is the escape hatch - and a bug report.
              name: 'POSTGRES_TLS_DISABLE_HOST_VERIFICATION'
              value: 'false'
            }
            {
              name: 'POSTGRES_TLS_SERVER_NAME'
              value: postgres.properties.fullyQualifiedDomainName
            }
            // ----------------------------------------------------------------
            // The SQL_* twins of the POSTGRES_TLS_* block above. Both are
            // required; they are not aliases and neither one implies the
            // other.
            //
            // temporalio/auto-setup runs two phases with two entirely
            // separate configuration paths, and only the first one reads
            // POSTGRES_TLS_*:
            //
            //   phase 1, schema  /etc/temporal/auto-setup.sh turns
            //                    POSTGRES_TLS_ENABLED / _DISABLE_HOST_
            //                    VERIFICATION / _SERVER_NAME into --tls*
            //                    flags on temporal-sql-tool.
            //   phase 2, server  /etc/temporal/entrypoint.sh renders
            //                    config_template.yaml with dockerize, and
            //                    that template reads SQL_TLS_ENABLED,
            //                    SQL_HOST_VERIFICATION and SQL_HOST_NAME.
            //                    POSTGRES_TLS_* appears nowhere in it.
            //
            // With only the POSTGRES_TLS_* half set, the datastore stanza
            // renders tls.enabled: false, the server dials Azure Postgres in
            // plaintext, Azure refuses it because SSL is mandatory there, and
            // the container dies in ServerOptionsProvider with "sql schema
            // version compatibility check failed: unable to read DB schema
            // version ... no usable database connection found". The schema
            // phase having just succeeded against the same server is what
            // makes it read like a schema problem instead of a TLS one.
            {
              name: 'SQL_TLS_ENABLED'
              value: 'true'
            }
            {
              // Named for the positive, unlike the setup phase's
              // POSTGRES_TLS_DISABLE_HOST_VERIFICATION. 'true' here and
              // 'false' there are the same posture: verify the certificate.
              name: 'SQL_HOST_VERIFICATION'
              value: 'true'
            }
            {
              // Must match the certificate Azure presents, so it is the
              // server FQDN and not the connectAddr host if those ever
              // diverge.
              name: 'SQL_HOST_NAME'
              value: postgres.properties.fullyQualifiedDomainName
            }
            {
              name: 'DBNAME'
              value: temporalDatabaseName
            }
            {
              name: 'VISIBILITY_DBNAME'
              value: temporalVisibilityDatabaseName
            }
            {
              // ARM already created both databases. Leaving this false would
              // have temporal-sql-tool issue CREATE DATABASE on every restart.
              name: 'SKIP_DB_CREATE'
              value: 'true'
            }
            {
              name: 'SKIP_DEFAULT_NAMESPACE_CREATION'
              value: 'false'
            }
            {
              name: 'DEFAULT_NAMESPACE'
              value: temporalNamespace
            }
            {
              name: 'DEFAULT_NAMESPACE_RETENTION'
              value: temporalNamespaceRetention
            }
            {
              name: 'NUM_HISTORY_SHARDS'
              value: string(temporalHistoryShards)
            }
            {
              // The entrypoint derives TEMPORAL_BROADCAST_ADDRESS from the
              // container's own address when this is 0.0.0.0, so ringpop still
              // advertises something routable. Binding to a single interface
              // instead is how you get an ingress that cannot reach the app.
              name: 'BIND_ON_IP'
              value: '0.0.0.0'
            }
            {
              // Used by the CLI inside the container for namespace creation,
              // not by anything outside it.
              name: 'TEMPORAL_ADDRESS'
              value: '127.0.0.1:7233'
            }
            {
              name: 'LOG_LEVEL'
              value: 'info'
            }
          ]
          resources: {
            cpu: temporalCpu
            memory: temporalMemory
          }
          probes: [
            {
              // Schema setup runs before the frontend listens. 300s of grace.
              type: 'Startup'
              tcpSocket: {
                port: 7233
              }
              periodSeconds: 10
              failureThreshold: 30
            }
            {
              type: 'Readiness'
              tcpSocket: {
                port: 7233
              }
              periodSeconds: 15
              failureThreshold: 10
            }
            {
              type: 'Liveness'
              tcpSocket: {
                port: 7233
              }
              initialDelaySeconds: 60
              periodSeconds: 30
              failureThreshold: 5
            }
          ]
        }
      ]
      // Pinned at exactly one. auto-setup is not safe to run concurrently -
      // two replicas racing setup-schema on the same database is a corrupted
      // cluster - and Temporal's own scaling story is more history nodes, not
      // more all-in-one containers.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    temporalDatabase
    temporalVisibilityDatabase
    postgresAllowAzure
    acrPull
  ]
}

// --------------------------------------------------------------------------
// AKS - Bot Desktop pool (optional)
// --------------------------------------------------------------------------

resource aks 'Microsoft.ContainerService/managedClusters@2024-09-01' = if (deployBotDesktopAks) {
  name: '${namePrefix}-aks'
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  sku: {
    name: 'Base'
    // Sizing, not hardening: the Standard tier buys an SLA on the API
    // server, not a stricter posture.
    tier: isFull ? 'Standard' : 'Free'
  }
  properties: {
    dnsPrefix: '${namePrefix}-aks'
    enableRBAC: true
    disableLocalAccounts: false
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: 1
        vmSize: 'Standard_D2as_v5'
        osType: 'Linux'
        osSKU: 'AzureLinux'
        type: 'VirtualMachineScaleSets'
        osDiskSizeGB: 64
      }
      {
        name: 'desktops'
        mode: 'User'
        count: botDesktopNodeCount
        minCount: 0
        maxCount: max(botDesktopNodeCount, 5)
        enableAutoScaling: true
        vmSize: botDesktopNodeSize
        osType: 'Linux'
        osSKU: 'AzureLinux'
        type: 'VirtualMachineScaleSets'
        osDiskSizeGB: 128
        // Matches the nodeSelector/tolerations in
        // infra/bot-desktop/k8s/desktop-template.yaml.
        nodeLabels: {
          workload: 'bot-desktop'
        }
        nodeTaints: [
          'workload=bot-desktop:NoSchedule'
        ]
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      // The desktop NetworkPolicy is only enforced with a policy engine.
      networkPolicy: 'calico'
      loadBalancerSku: 'standard'
    }
    addonProfiles: {
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalytics.id
        }
      }
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: {
          enableSecretRotation: 'true'
        }
      }
    }
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    autoUpgradeProfile: {
      upgradeChannel: 'patch'
    }
  }
}

// AKS pulls desktop images with the same identity.
resource aksAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployBotDesktopAks) {
  scope: acr
  name: guid(acr.id, '${namePrefix}-aks', roleAcrPull)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
  }
}

// --------------------------------------------------------------------------
// Outputs - every endpoint a deployer or a pipeline needs
// --------------------------------------------------------------------------

output environmentName string = environmentName
output sizingTierOut string = sizingTier
output location string = location
output resourceGroupName string = resourceGroup().name

#disable-next-line BCP318 // guarded by the ternary above
output apiFqdn string = deployApps ? apiApp.properties.configuration.ingress.fqdn : ''
#disable-next-line BCP318 // guarded by the ternary above
output apiUrl string = deployApps ? 'https://${apiApp.properties.configuration.ingress.fqdn}' : ''
output apiAppName string = deployApps ? apiApp.name : ''
output workerAppName string = deployApps ? workerApp.name : ''
output containerAppsEnvironmentName string = cae.name

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer

output managedIdentityName string = uami.name
output managedIdentityClientId string = uami.properties.clientId
output managedIdentityPrincipalId string = uami.properties.principalId
output managedIdentityResourceId string = uami.id

output postgresName string = postgres.name
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output postgresDatabase string = postgresDatabaseName
output postgresAdminLoginOut string = postgresAdminLogin
@description('Connection string with the password redacted. The real one lives in Key Vault as `database-url`.')
output databaseUrlTemplate string = 'postgresql+asyncpg://${postgresAdminLogin}:<password>@${postgres.properties.fullyQualifiedDomainName}:5432/${postgresDatabaseName}?ssl=require'

@description('containerApp on the lean tier, redisEnterprise on full.')
output redisMode string = isFull ? 'redisEnterprise' : 'containerApp'
output redisName string = isFull ? redisEnterpriseName : redisAppName
// Lean emits the bare app name, not the .internal FQDN: this is a TCP
// ingress and the FQDN form points at the environment's shared HTTP proxy.
// See redisInternalEndpoint. An operator copying this value has to get a
// reachable one.
#disable-next-line BCP318 // guarded by the ternary
output redisHost string = isFull ? redisEnterprise.properties.hostName : redisAppName
@description('Lean: the real URL, no credential in it. Full: the key is redacted - the real one lives in Key Vault as `redis-url`.')
output redisUrlTemplate string = isFull ? 'rediss://:<primary-key>@${redisEnterpriseName}.${location}.redisenterprise.cache.azure.net:10000/0' : redisInternalEndpoint

output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri

output storageAccountName string = storage.name
output blobEndpoint string = storage.properties.primaryEndpoints.blob
output fileEndpoint string = storage.properties.primaryEndpoints.file
output botHomesShareName string = botHomesShare.name
output artifactsContainerName string = artifactsContainer.name
output screenshotsContainerName string = screenshotsContainer.name

output openaiEndpoint string = openai.properties.endpoint
output openaiAccountName string = openai.name
output openaiDeployments object = {
  nano: deployNano.name
  mini: deployMini.name
  reason: deployReason.name
  embed: deployEmbed.name
}

output logAnalyticsWorkspaceId string = logAnalytics.id
output appInsightsName string = appInsights.name
@description('App Insights connection string. Contains an instrumentation key - treat as a low-sensitivity secret.')
output appInsightsConnectionString string = appInsights.properties.ConnectionString

output vnetName string = vnet.name

@description('Bot Desktop plumbing. These map one-for-one onto the aci_* settings in apps/api/app/config.py.')
output aciResourceGroup string = resourceGroup().name
output aciSubscriptionId string = subscription().subscriptionId
output aciRegion string = location
output aciSubnetId string = aciSubnet.id
output aciRegistryServer string = acr.properties.loginServer
@description('Full ARM resource id of the user-assigned identity - what ACI expects, not the client id.')
output aciRegistryIdentity string = uami.id
output botDesktopMode string = botDesktopMode

@description('Azure Files mount for bot homes. ACI has no identity-based SMB option, so the driver needs the account key; it is in Key Vault under aciFilesKeySecretName.')
output aciFilesAccount string = storage.name
output aciFilesShare string = botHomesShare.name
output aciFilesKeySecretName string = kvStorageAccountKey.name

#disable-next-line BCP318 // guarded by the ternary
output aciEgressPublicIp string = deployAciNatGateway ? aciNatPublicIp.properties.ipAddress : ''

output temporalDeployed bool = deployTemporal
output temporalAppNameOut string = deployTemporal ? temporalAppName : ''
@description('host:port the API and worker use. Internal ingress - it does not resolve outside the Container Apps environment.')
output temporalEndpointOut string = temporalEndpoint
output temporalNamespaceOut string = temporalNamespace

output aksName string = deployBotDesktopAks ? aks.name : ''
#disable-next-line BCP318 // guarded by the ternary above
output aksOidcIssuerUrl string = deployBotDesktopAks ? aks.properties.oidcIssuerProfile.issuerURL : ''
output botDesktopImage string = '${acr.properties.loginServer}/nesqbot/bot-desktop:${botDesktopTag}'

@description('Paste into .env / Container App config after deployment.')
output envSummary object = {
  AZURE_OPENAI_ENDPOINT: openai.properties.endpoint
  AZURE_KEY_VAULT_URL: kv.properties.vaultUri
  AZURE_TENANT_ID: entraTenantId
  AZURE_CLIENT_ID: entraApiAppId
  AZURE_API_SCOPE: entraApiScope
  AZURE_MANAGED_IDENTITY_CLIENT_ID: uami.properties.clientId
  CORS_ORIGINS: join(allowedOrigins, ',')
  BOT_DESKTOP_MODE: botDesktopMode
  BOT_DESKTOP_IMAGE: '${acr.properties.loginServer}/nesqbot/bot-desktop:${botDesktopTag}'
  ACI_RESOURCE_GROUP: resourceGroup().name
  ACI_SUBSCRIPTION_ID: subscription().subscriptionId
  ACI_REGION: location
  ACI_SUBNET_ID: aciSubnet.id
  ACI_REGISTRY_SERVER: acr.properties.loginServer
  ACI_REGISTRY_IDENTITY: uami.id
  ACI_FILES_ACCOUNT: storage.name
  ACI_FILES_SHARE: botHomesShare.name
  ACI_FILES_KEY_SECRET_NAME: kvStorageAccountKey.name
  TEMPORAL_HOST: temporalEndpoint
  TEMPORAL_NAMESPACE: temporalNamespace
}

@description('Build-time configuration for the desktop and mobile clients. EXPO_PUBLIC_* values are inlined into the bundle, so none of them may ever be a secret.')
output clientEnvSummary object = {
  EXPO_PUBLIC_ENTRA_TENANT_ID: entraTenantId
  EXPO_PUBLIC_ENTRA_CLIENT_ID: entraClientId
  EXPO_PUBLIC_ENTRA_SCOPE: empty(entraApiAppId) ? '' : 'api://${entraApiAppId}/${entraApiScope}'
  EXPO_PUBLIC_ENTRA_REDIRECT_URI: clientRedirectUri
  #disable-next-line BCP318 // guarded by the ternary
  EXPO_PUBLIC_API_BASE_URL: deployApps ? 'https://${apiApp.properties.configuration.ingress.fqdn}' : ''
}

@description('Redirect URI to register on the Entra app registration. Must match what the mobile Settings screen displays, character for character.')
output clientRedirectUriOut string = clientRedirectUri

@description('Reminder: apps/mobile/app.json needs expo.extra.eas.projectId (npx eas init) or push notifications for approvals silently do nothing.')
output manualSteps array = [
  'Create the Entra app registration: platform "Mobile and desktop applications", redirect URI ${clientRedirectUri}, ID tokens enabled. Feed the client id back in as entraClientId.'
  'Run `npx eas init` in apps/mobile to populate expo.extra.eas.projectId - required for approval push notifications.'
  'Run apps/api/sql/init.sql once against the nesqbot database: the template allow-lists pgvector at the server level, but CREATE EXTENSION vector still has to happen inside the database.'
  'Register Microsoft.ContainerInstance on the subscription if it is not already: az provider register -n Microsoft.ContainerInstance --wait'
]
