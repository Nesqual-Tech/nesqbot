# Azure deployment

`main.bicep` provisions the whole Nesq Bot stack into one resource group.

**Region: `swedencentral`.** EU data residency is a product differentiator, not
a preference, so it is not a knob to twiddle. All four Foundry models below were
verified present there as `GlobalStandard` with
`az cognitiveservices model list -l swedencentral`. Check before moving region:
`gpt-5.6-luna` and `gpt-5.6-sol` are not in every European region, and
`text-embedding-3-small` is offered in Sweden Central as `GlobalStandard` and
`DataZoneStandard` only — plain `Standard` does not exist there.

## The two axes

The single most important thing to understand about this template:

| Parameter         | Controls                                                            | Costs money |
| ----------------- | ------------------------------------------------------------------- | ----------- |
| `environmentName` | Hardening: `NESQ_ENV`, CORS, purge protection, retention windows     | No          |
| `sizingTier`      | Spend: Postgres/Redis SKUs, replica floors, container CPU and memory | Yes         |

They used to be the same flag, which meant you could not run production
behaviour without also buying production hardware. The supported production
shape for an internal team is `environmentName='prod'` **and**
`sizingTier='lean'`: no dev-login, no localhost CORS, purge-protected vault,
90-day logs, 35-day backups — on a Burstable B2s and a Basic C0.

Nothing in the template may read `isProd` to pick a SKU or `isFull` to pick a
security setting. If you add a resource, put each of its properties on the
correct axis.

Two things that look like hardening live on the sizing axis anyway, because the
platform ties them to the SKU: Postgres **zone-redundant HA** and
**geo-redundant backup** are GeneralPurpose-and-up features. Asking for them on
a Burstable server is a deployment failure, not a stricter posture.

`sizingTier` also picks which of two completely different Redis
implementations you get — see below. That is the one place where the two tiers
are not the same component at different sizes.

## What it creates

| Resource                             | Notes                                                                        |
| ------------------------------------ | ---------------------------------------------------------------------------- |
| User-assigned managed identity       | Shared by API and worker; holds every RBAC grant                             |
| Virtual network                      | `snet-container-apps` (delegated to Container Apps), `snet-bot-desktops`     |
| NSG on the desktop subnet            | In: 6901/7910 from the Container Apps subnet. Out: 53/80/443 to internet only |
| NAT gateway + static public IP       | Outbound egress for the desktop subnet — **required**, see below            |
| Log Analytics + Application Insights | 30 d retention, 90 d when `environmentName='prod'`                            |
| Key Vault                            | RBAC-authorised; seeded with six secrets                                     |
| Storage account                      | Blob `artifacts` + `screenshots`, file share `bot-homes`                     |
| Container Registry                   | Admin user disabled — every pull uses the managed identity                   |
| PostgreSQL Flexible Server 16        | `azure.extensions` allow-lists `VECTOR,PGCRYPTO,PG_STAT_STATEMENTS,UUID-OSSP,BTREE_GIN,BTREE_GIST`; databases `nesqbot`, `temporal`, `temporal_visibility` |
| Redis                                | lean: Container App, internal TCP 6379, no persistence. full: Azure Managed Redis |
| Azure OpenAI                         | `gpt-5.6-luna`, `gpt-5.4-mini`, `gpt-5.6-sol`, `text-embedding-3-small`      |
| Container Apps environment           | Consumption profile, VNet-integrated, Azure Files mount for bot homes        |
| Container App `api`                  | External ingress, probes, HTTP autoscale                                     |
| Container App `worker`               | No ingress                                                                   |
| Container App `temporal`             | **Internal** TCP ingress on 7233, `temporalio/auto-setup` pinned             |
| AKS                                  | Off, and staying off — ACI replaces it                                       |

## Parameters

| Parameter                   | Default                          | Notes                                            |
| --------------------------- | -------------------------------- | ------------------------------------------------ |
| `environmentName`           | `dev`                            | `dev` \| `staging` \| `prod`. Hardening only     |
| `sizingTier`                | `lean`                           | `lean` \| `full`. Spend only                     |
| `location`                  | resource group location          | Pin to `swedencentral`                           |
| `suffix`                    | `uniqueString(rg.id)`            | Globally-unique name seed                        |
| `postgresAdminLogin`        | `pgadmin`                      |                                                  |
| `postgresAdminPassword`     | **none — required**              | `@secure`, min 16                                |
| `jwtSecret`                 | **none — required**              | `@secure`, min 32                                |
| `workerApiToken`            | **none — required**              | `@secure`, min 32                                |
| `sidecarToken`              | **none — required**              | `@secure`, min 24                                |
| `entraTenantId`             | deploying tenant                 |                                                  |
| `entraClientId`             | `''`                             | Manual Entra app registration; see below         |
| `clientRedirectUri`         | `nesqbot://auth`                 |                                                  |
| `deployTemporal`            | `true`                           | Self-hosted Temporal Container App               |
| `redisImage`                | `redis:7.4-alpine`               | Lean tier only. Matches `docker-compose.yml`     |
| `temporalImage`             | `temporalio/auto-setup:1.25.2`   | Matches `docker-compose.yml`                     |
| `temporalHost`              | `''`                             | Override to use Temporal Cloud instead           |
| `temporalNamespace`         | `default`                        |                                                  |
| `temporalNamespaceRetention`| `720h`                           |                                                  |
| `temporalHistoryShards`     | `128`                            | **Immutable** after first schema setup           |
| `botDesktopMode`            | `aci`                            | `mock` \| `aci` \| `aks`                         |
| `aciCpu` / `aciMemoryGb`    | `2` / `4`                        | Per desktop container group                      |
| `vnetAddressPrefix`         | `10.60.0.0/16`                   | Change if it collides with anything peered       |
| `containerAppsSubnetPrefix` | `10.60.0.0/23`                   | Delegated `Microsoft.App/environments`           |
| `aciSubnetPrefix`           | `10.60.4.0/23`                   | Delegated `Microsoft.ContainerInstance/containerGroups` |
| `deployAciNatGateway`       | `true`                           | Egress for the desktop subnet. Turning it off breaks ACR pulls |
| `apiAllowedOrigins`         | `[]`                             | Empty derives per-environment defaults           |
| `imageTag`                  | `v0.1.0`                         | Never `latest`                                   |
| `deployApps`                | `true` (`false` in `prod.bicepparam`) | See "First deploy" below                    |
| `deployBotDesktopAks`       | `false`                          | Leave it                                         |
| `botDesktopNodeCount`       | `1`                              | AKS only                                         |
| `botDesktopNodeSize`        | `Standard_D4as_v5`               | AKS only                                         |
| `tags`                      | `{}`                             | Merged onto every resource                       |

### Secrets

`postgresAdminPassword`, `jwtSecret`, `workerApiToken` and `sidecarToken` are
`@secure()` with **no default anywhere** — not in `main.bicep`, not in either
`.bicepparam`. A missing one fails at `az bicep build-params` time. Because
they are `@secure()`, ARM redacts them from deployment history, what-if output
and `az deployment group show`; `outputs` never contains any of them, and the
two connection-string outputs are templates with `<password>` / `<primary-key>`
placeholders.

All four land in Key Vault, alongside the two composed connection strings:

| Key Vault secret          | Source                        |
| ------------------------- | ----------------------------- |
| `postgres-admin-password` | `postgresAdminPassword`       |
| `jwt-secret`              | `jwtSecret`                   |
| `worker-api-token`        | `workerApiToken`              |
| `sidecar-token`           | `sidecarToken`                |
| `database-url`            | composed, password URI-encoded |
| `redis-url`               | composed, key URI-encoded      |
| `storage-account-key`     | `storage.listKeys()` — ACI Azure Files mounts |

Rotate them in the vault; the vault is the single home.

### RBAC granted to the managed identity

| Role                                                | Scope                    | Why                                            |
| --------------------------------------------------- | ------------------------ | ---------------------------------------------- |
| AcrPull                                             | ACR                      | Image pulls without admin credentials          |
| Key Vault Secrets User                              | Key Vault                | Connector secrets at runtime                   |
| Storage Blob Data Contributor                       | Storage                  | Artifacts and screenshots                      |
| Storage File Data SMB Share Contributor             | Storage                  | Bot home shares                                |
| Cognitive Services OpenAI User                      | Azure OpenAI             | Token auth instead of an API key               |
| Monitoring Metrics Publisher                        | App Insights             | Custom metrics                                 |
| **Azure Container Instances Contributor Role**      | **resource group**       | Create / read / delete Bot Desktop container groups |
| **Nesq Bot Desktop Subnet Join** (custom)           | **`snet-bot-desktops`**  | Join container groups to the delegated subnet  |
| **Managed Identity Operator**                       | **the identity itself**  | Attach that identity to the container groups it creates |

Spawning a desktop is three separate authorisation decisions and only the first
is obvious:

1. **containerGroups CRUD.** `Azure Container Instances Contributor Role`
   (`5d977122-f97e-4b4d-a52f-6b43003ddb4d`) is the narrowest built-in that
   exists for this: `Microsoft.ContainerInstance/containerGroups/*` plus reads,
   and it cannot see any other provider. Scoped to the resource group, because
   the container groups do not exist yet and you cannot scope to a resource you
   have not created. Contributor would also have worked and would also have let
   a prompt-injected desktop driver delete the Postgres server.
2. **Subnet join.** ARM requires
   `Microsoft.Network/virtualNetworks/subnets/join/action` on the target subnet
   before it will place anything in it. The built-in option is Network
   Contributor, which also carries `subnets/delete`, so the template defines a
   two-action custom role scoped to the one subnet instead.
3. **Identity attach.** Without `Managed Identity Operator` the container group
   cannot carry a user-assigned identity, and without a user-assigned identity
   the only remaining way to pull the desktop image is the ACR admin user.
   Scoped to this one identity, so it cannot attach any other.

The custom role definition means the deploying principal needs
`Microsoft.Authorization/roleDefinitions/write` — Owner or User Access
Administrator. It already needed `roleAssignments/write` for the six original
grants, so this is not a new class of permission.

## Deploy

The subscription needs the ACI provider registered. It is a one-time,
subscription-wide, no-op-if-already-done operation:

```bash
az provider register -n Microsoft.ContainerInstance --wait
```

### Production

```powershell
az group create -n rg-nesqbot -l swedencentral

$env:NESQBOT_PG_PASSWORD   = python -c "import secrets;print(secrets.token_urlsafe(32))"
$env:NESQBOT_JWT_SECRET    = python -c "import secrets;print(secrets.token_urlsafe(48))"
$env:NESQBOT_WORKER_TOKEN  = python -c "import secrets;print(secrets.token_urlsafe(48))"
$env:NESQBOT_SIDECAR_TOKEN = python -c "import secrets;print(secrets.token_urlsafe(32))"

# Always look before you leap.
az deployment group what-if -g rg-nesqbot `
  -f infra/azure/main.bicep -p infra/azure/prod.bicepparam

az deployment group create -g rg-nesqbot -n main `
  -f infra/azure/main.bicep -p infra/azure/prod.bicepparam
```

`prod.bicepparam` pins `environmentName='prod'`, `sizingTier='lean'`,
`location='swedencentral'`, `botDesktopMode='aci'` and — deliberately —
`deployApps=false`.

### First deploy: ACR is empty

The api and worker Container Apps pull `nesqbot/api:$imageTag` from an ACR that
has nothing in it until you push, so the first pass leaves them out. Everything
else comes up: VNet and both subnets, Postgres with all three databases, Redis,
Key Vault with all six secrets, Storage, ACR, the four Foundry deployments,
every RBAC grant, and Temporal — which pulls a public image and needs nothing
from ACR, so it can run its schema setup on this pass.

```powershell
# 1. Data plane + registry, no apps. This is the default in prod.bicepparam.
az deployment group create -g rg-nesqbot -n main `
  -f infra/azure/main.bicep -p infra/azure/prod.bicepparam

# 2. Push the three images.
#
#    The api image MUST be built with --build-arg NESQ_BUILD=<its own tag>. That
#    value is what GET /api/health reports as "build" and what the desktop
#    footer shows. Omit it and the footer falls back to the hand-maintained
#    contract number, which is how a healthy v0.3.0 deploy came to read
#    "API 0.2.0" and look like a failure.
$ACR = az deployment group show -g rg-nesqbot -n main `
  --query properties.outputs.acrLoginServer.value -o tsv
az acr login -n $ACR.Split('.')[0]
docker build --build-arg NESQ_BUILD=v0.1.0 -t "$ACR/nesqbot/api:v0.1.0" apps/api; docker push "$ACR/nesqbot/api:v0.1.0"
docker build -t "$ACR/nesqbot/worker:v0.1.0" apps/worker; docker push "$ACR/nesqbot/worker:v0.1.0"
docker build -t "$ACR/nesqbot/bot-desktop:v0.1.0" infra/bot-desktop; docker push "$ACR/nesqbot/bot-desktop:v0.1.0"

# 3. Same command, apps on.
$env:NESQBOT_DEPLOY_APPS = "true"
az deployment group create -g rg-nesqbot -n main `
  -f infra/azure/main.bicep -p infra/azure/prod.bicepparam
```

`.github/workflows/docker.yml` pushes those three images on a tag.

### After the deploy

```bash
az deployment group show -g rg-nesqbot -n main --query properties.outputs
```

`aciResourceGroup`, `aciSubscriptionId`, `aciRegion`, `aciSubnetId`,
`aciRegistryServer` and `aciRegistryIdentity` map one-for-one onto the `aci_*`
settings in `apps/api/app/config.py`. The Bicep already sets them as env vars on
the API Container App; the outputs are for `.env` files and for reading back.

`CREATE EXTENSION vector` still has to run once inside the database — the Bicep
only allow-lists it at the server level:

```bash
psql "host=<postgresFqdn> user=pgadmin dbname=nesqbot sslmode=require" \
  -f apps/api/sql/init.sql
```

## Temporal

`temporalio/auto-setup:1.25.2` — the same tag `docker-compose.yml` runs, so dev
and production are the same server version. It talks to the `temporal` and
`temporal_visibility` databases on the Postgres Flexible Server this template
creates, over TLS with host verification on.

- **Internal ingress only.** `external: false`, `transport: tcp`, port 7233.
  Nothing outside the Container Apps environment resolves the name and there is
  no public route to it. The API and worker reach it at the **bare app name**,
  `nesqbot-<env>-temporal:7233` — not at an `.internal.<default-domain>` FQDN.
  That distinction is load-bearing and it fails silently: for **TCP** ingress
  the `<app>.internal.<defaultDomain>` name resolves to the environment's
  shared HTTP edge proxy — every app in the environment resolves to the *same*
  address — and that proxy only speaks HTTP/HTTPS, so a gRPC dial to it hangs
  until the client's timeout instead of being refused. Only the bare app name
  resolves to the app's own service IP, and it exists only because the ingress
  block sets `exposedPort` as well as `targetPort`. The same rule applies to
  Redis on the lean tier. Using the app name also keeps the template from
  referencing the Temporal resource, which would make both apps wait on
  Temporal at deploy time; both are written to treat an unreachable Temporal as
  degraded rather than fatal, and nothing depends on its readiness.
- **`minReplicas` and `maxReplicas` are both 1, on purpose.** `auto-setup` is
  not safe to run concurrently: two replicas racing `setup-schema` against the
  same database is a corrupted cluster. Temporal's scaling story is more history
  nodes, not more all-in-one containers.
- **ARM creates the databases, Temporal creates the schema.** `SKIP_DB_CREATE=true`,
  so `temporal-sql-tool` never issues `CREATE DATABASE`; `setup-schema` and
  `update-schema` still run on every start and are idempotent.
- **`NUM_HISTORY_SHARDS=128` is immutable.** Changing it later means a fresh
  cluster and a namespace migration, not a redeploy. 128 is sized for a
  burstable Postgres; Temporal suggests 512 for clusters that will see that load.
- **`BIND_ON_IP=0.0.0.0`.** The image's entrypoint derives
  `TEMPORAL_BROADCAST_ADDRESS` from the container's own address when it sees
  `0.0.0.0`, so ringpop still advertises something routable.
- **No `temporal-ui`.** The compose stack has one; production does not. Adding
  it means another always-on Container App and a public ingress in front of a
  console that can terminate workflows.
- **Docker Hub rate limits.** The image is pulled anonymously from Docker Hub.
  If that ever bites, mirror it and point `temporalImage` at the copy — the app
  already carries the ACR registry credential:
  ```bash
  az acr import -n <acr> --source docker.io/temporalio/auto-setup:1.25.2 \
    -t temporalio/auto-setup:1.25.2
  ```
- **TLS to Postgres needs BOTH the `POSTGRES_TLS_*` and the `SQL_*` env
  vars.** They are not aliases and neither implies the other, because
  `auto-setup` configures its two phases from two unrelated places:
  `/etc/temporal/auto-setup.sh` turns `POSTGRES_TLS_ENABLED`,
  `POSTGRES_TLS_DISABLE_HOST_VERIFICATION` and `POSTGRES_TLS_SERVER_NAME` into
  `--tls*` flags on `temporal-sql-tool` (the **schema** phase), while
  `/etc/temporal/entrypoint.sh` renders `config_template.yaml` with
  `dockerize`, and that template reads `SQL_TLS_ENABLED`,
  `SQL_HOST_VERIFICATION` and `SQL_HOST_NAME` (the **server** phase).
  `POSTGRES_TLS_*` appears nowhere in the template. Set only the first half and
  the datastore stanza renders `tls.enabled: false`, the server dials Azure
  Postgres in plaintext, Azure refuses it because SSL is mandatory, and the
  container dies in `ServerOptionsProvider` with *"sql schema version
  compatibility check failed: unable to read DB schema version … no usable
  database connection found"*. The schema phase having just succeeded against
  the same server is what makes that read like a schema problem instead of a
  TLS one. Both halves are set in `main.bicep`; keep them in step.
- If `temporal-sql-tool` ever fails the TLS handshake against Postgres, the
  escape hatch is `POSTGRES_TLS_DISABLE_HOST_VERIFICATION=true` (and its server
  -phase twin `SQL_HOST_VERIFICATION=false`) — and a bug report, because the
  image trusts the system root store and Azure presents a certificate for the
  server FQDN.

## Redis

### Two retirements, and a SKU list that lies

`Microsoft.Cache/redis` (Azure Cache for Redis) is **retired** — the control
plane refuses new creations outright:

```
BadRequest: Azure Cache for Redis is retiring, create Azure Managed Redis
instance instead. https://aka.ms/AzureCacheForRedisRetirement
```

So is the **Enterprise family** on `Microsoft.Cache/redisEnterprise`:

```
BadRequest: Creation of new Azure Cache for Redis Enterprise resources is no
longer supported. Azure Cache for Redis Enterprise is retiring, please create
Azure Managed Redis instead.
```

Azure Managed Redis is the survivor, and confusingly it lives on that *same*
`redisEnterprise` resource type. What makes a deployment AMR rather than
Enterprise is the SKU family: `Balanced_B*`, `MemoryOptimized_M*`,
`ComputeOptimized_X*` are AMR; `Enterprise_E*` and `EnterpriseFlash_F*` are
the retiring product.

> **Do not trust `Microsoft.Cache/skus` for this.** For `swedencentral` it
> lists *only* the retiring Enterprise families and omits every AMR family, on
> `2024-11-01`, `2025-08-01-preview` and `2026-06-01-preview` alike.
> `Balanced_B0` nevertheless passes preflight in this region. The list is stale,
> not authoritative — preflight the SKU instead:
>
> ```bash
> az deployment group validate -g <throwaway-rg> --template-file probe.json \
>   --parameters skuName=Balanced_B0
> ```
>
> Also note `properties.publicNetworkAccess` is **required** from api-version
> `2025-07-01`; omitting it fails preflight rather than defaulting.

### Why lean is still a container

Not because managed Redis is unavailable — `Balanced_B0` is real and costs
roughly USD 55/month. Because roughly USD 6–20/month for a container is a
better trade for a component that stores nothing, on a stack already over its
target. It is a judgement call, and it is reversible: point `redisUrl` at an
AMR instance and the app tier does not change.

The container, concretely: same pattern as Temporal — `redis:7.4-alpine` (matching `docker-compose.yml`;
`7-alpine` floats across minor versions and is not a pin), internal TCP ingress
on 6379, 0.25 vCPU / 0.5 GiB, exactly one replica.

That is defensible **here specifically** because Redis is not a datastore in
this system. `app/services/events.py` uses it for SSE pub/sub fanout and
already falls back to an in-process `asyncio.Queue`. Nothing durable lives in
it.

**There is no persistence, deliberately.** No volume, `--save ""` so RDB
snapshotting is off, `--appendonly no`. A restart loses everything in it. Do
not put a queue, a session store, a lock, or a cache that anything depends on
surviving into this instance without revisiting the decision first — by the
time you notice, the restart will already have happened.

It is still not optional. With more than one API replica, in-process fanout
alone means a client connected to replica A never sees an event published on
replica B. Removing Redis would silently cap the API at one replica, which is
a worse failure than the cost it saves because nothing reports it.

Three details that are load-bearing:

- **The apps address it as `nesqbot-<env>-redis:6379`, NOT as
  `nesqbot-<env>-redis.internal.<default-domain>:6379`.** This is the one real
  difference between HTTP and TCP ingress, and it presents as a hang rather
  than an error, which is why it is easy to lose an afternoon to. The
  `.internal.<defaultDomain>` name resolves to the environment's shared HTTP
  edge proxy — *every* app in the environment resolves to the same address —
  and that proxy only speaks HTTP/HTTPS, so a RESP connection to it is accepted
  by nothing and stalls until the client times out (`redis: error: TimeoutError`
  out of `/api/health/deep`, against a Redis revision reporting perfectly
  Healthy). The bare app name resolves to the app's own service IP, and that
  address exists only because the ingress block sets `exposedPort` in addition
  to `targetPort`. Verified from inside the API container: the FQDN resolved to
  the shared proxy IP and timed out; `nesqbot-redis` resolved to a
  different, per-app IP and answered `PING` with `+PONG`. Same rule for
  Temporal.
- **`--protected-mode no`.** With no `bind` directive and no password, Redis
  refuses every non-loopback connection, including the Container Apps ingress
  proxy. This is only safe because ingress is internal.
- **`maxReplicas: 1`.** Two replicas would be two unrelated Redis processes
  behind one name, and a subscriber on one would never see a publish on the
  other — precisely the bug this component exists to prevent.

No password and no TLS on the wire. The endpoint is internal-ingress only, so
it resolves nowhere outside the Container Apps environment, and the ACI desktop
subnet is denied outbound to RFC1918 and cannot reach it. Same posture as
Temporal, which holds rather more sensitive state.

### Full: Azure Managed Redis

`sizingTier='full'` provisions `Balanced_B1` (1 GB) with
`highAvailability: Enabled` and an `EnterpriseCluster` database on port 10000 —
replicated and zone redundant, which a single container is not. Roughly USD
90/month, so it stays an explicit branch rather than something prod falls into
by accident.

`EnterpriseCluster` rather than `OSSCluster` gives one endpoint behind a proxy,
which is what a non-cluster-aware client like `redis-py` expects.

The `redis-url` Key Vault secret is repointed, not removed. On lean it holds a
plain `redis://…internal…:6379/0` with no credential in it; on full it carries
the Enterprise access key and is a secret in earnest. Same name either way, so
nothing downstream has to care which tier it is running on.

## Bot Desktops on ACI

One container group per bot: hypervisor-isolated, its own filesystem and
identity, billed per second, no cluster floor. This is why there is no AKS.

- Injected into `snet-bot-desktops`, delegated **exclusively** to
  `Microsoft.ContainerInstance/containerGroups`. Container groups there get a
  private IP and **no public endpoint**. `/23` is 507 usable addresses, one per
  running group; IP space is not the ceiling (see Quota below).
- The Container Apps environment sits on `snet-container-apps` in the same VNet,
  so the API reaches a desktop by plain VNet routing — no public hop.
- Images come from ACR via the user-assigned identity's `AcrPull` grant.
  `adminUserEnabled` is `false` on the registry, so admin credentials are not
  merely discouraged, they do not exist. `ACI_REGISTRY_IDENTITY` is the
  identity's **full ARM resource id** — the ACI SDK keys both
  `userAssignedIdentities` and `ImageRegistryCredential.identity` by resource
  id, and a client id there fails at create time. `BOT_DESKTOP_IMAGE` is
  likewise fully qualified as `<acrLoginServer>/nesqbot/bot-desktop:<tag>`, so
  it sits under `ACI_REGISTRY_SERVER` and the pull identity actually applies.

### Egress: the failure nobody sees coming

**A delegated ACI subnet has no outbound internet by default.** Azure retired
default outbound access for new deployments on 2025-09-30, and a VNet-injected
container group cannot carry a public IP of its own. Without an explicit egress
path the ACR pull does not fail fast — it hangs until the container group start
timeout, and the only symptom is a desktop that never becomes ready.

The template therefore attaches a **NAT gateway with a static public IP** to the
desktop subnet (`deployAciNatGateway`, on by default). A NAT gateway rather
than a private endpoint for the registry, for two reasons: an ACR private
endpoint requires the **Premium** SKU, which on its own costs more per month
than the gateway; and a Bot Desktop is a browser whose whole job is reaching
third-party SaaS, so it needs general internet egress regardless. If you ever do
go private-endpoint-only on the registry, the `privatelink.azurecr.io` private
DNS zone **must** be linked to this VNet or the pull resolves to a public
address it cannot reach.

The egress IP is an output (`aciEgressPublicIp`) — useful if a vendor the bots
sign into wants an allow-list entry.

### Network policy

The NSG mirrors the NetworkPolicy the AKS path already had in
`infra/bot-desktop/k8s/desktop-template.yaml`:

| Dir | Prio | Rule                                                              |
| --- | ---- | ----------------------------------------------------------------- |
| In  | 100  | Allow 6901 (noVNC) + 7910 (sidecar) from the Container Apps subnet |
| In  | 4000 | Deny everything else from the VNet (overrides `AllowVnetInBound`)  |
| Out | 100  | Deny 169.254.0.0/16 (link-local / IMDS)                            |
| Out | 110  | Deny 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16                     |
| Out | 200  | Allow 53 to 168.63.129.16 (Azure platform DNS)                     |
| Out | 210  | Allow 80 + 443 to Internet                                         |
| Out | 220  | Allow 53 to Internet                                               |
| Out | 4000 | Deny everything else (overrides `AllowInternetOutBound`)           |

The RFC1918 deny covers this VNet too — 10.60.0.0/16 sits inside 10.0.0.0/8 —
so a prompt-injected page cannot reach Postgres, Redis, the API's internal
ingress, or another bot's desktop. Per-bot isolation is the differentiator; it
should hold at the network layer and not only in the orchestration story.

**When a desktop misbehaves, these two rules are the first and second place to
look**: `DenyEverythingElseFromVnet` if it is unreachable,
`DenyEverythingElseOutbound` if it starts but cannot fetch anything.

### Bot homes must be a volume

Without a mounted volume every stop destroys the bot's home directory — browser
profile, cookies and logged-in sessions with it — which undoes the one thing a
Bot Desktop exists to do. The share already exists and the API mounts it at
`/mnt/bot-homes`; desktops mount the same one, so what the API writes into a
bot's home is what the desktop sees.

| What                | Value                                              |
| ------------------- | -------------------------------------------------- |
| Storage account     | output `aciFilesAccount` (`nesq<env>st<suffix>`)   |
| File share          | `bot-homes` — output `aciFilesShare`               |
| Key Vault secret    | `storage-account-key` — output `aciFilesKeySecretName` |
| Share quota         | 100 GiB lean, 1024 GiB full                        |

ACI has no identity-based option for SMB volumes — unlike blob, an
`AzureFileVolume` takes the account key — so the key is seeded into Key Vault
and the driver reads it with the identity it already has (`Key Vault Secrets
User`, plus `AZURE_KEY_VAULT_URL` in its environment). The API Container App
also carries `ACI_FILES_ACCOUNT`, `ACI_FILES_SHARE` and
`ACI_FILES_KEY_SECRET_NAME` as plain env vars. The key itself is never an env
var.

> **One share, all bots.** `AzureFileVolume` mounts a whole share; it has no
> sub-path option. Every desktop that mounts `bot-homes` can therefore read
> every other bot's home directory, which is weaker than the per-bot boundary
> the network layer gives you. The fix is one share per bot, created on demand —
> the account key the driver already holds is enough to create shares through
> the Files data plane, no extra RBAC. Worth doing before this holds anything a
> customer would mind another bot reading.

### Quota

Checked against this subscription in `swedencentral` on 2026-08-22:

| Quota                       | Limit | Meaning                                    |
| --------------------------- | ----- | ------------------------------------------ |
| `ContainerGroups`           | 100   | Not the binding constraint                 |
| `StandardCores`             | **10**| **At `aciCpu=2`, five concurrent desktops** |
| `DedicatedContainerGroups`  | 0     | Not used                                   |
| `StandardSpotCores`         | 0     | Not used                                   |

`StandardCores` is the real ceiling and the default is low. Raise it through a
support request before onboarding more than four or five bots that run at once,
or drop `aciCpu` to 1. VNet-injected ACI (`ipAddressType: Private`) is confirmed
available in `swedencentral`; max 32 vCPU / 256 GiB per container group.

```bash
az rest --method get --url "https://management.azure.com/subscriptions/<sub>/providers/Microsoft.ContainerInstance/locations/swedencentral/usages?api-version=2021-09-01"
```

## Manual step: the client app registration

Bicep cannot create this. Entra app registrations are Microsoft Graph objects,
not ARM resources, so `main.bicep` only takes the resulting client id as the
`entraClientId` parameter.

Portal:

1. **Entra ID → App registrations → New registration** — name `Nesq Bot`,
   single tenant.
2. **Authentication → Add a platform → Mobile and desktop applications** —
   custom redirect URI `nesqbot://auth`. The mobile Settings screen displays
   this string verbatim so whoever does the registration can copy it exactly.
3. **Authentication → Implicit grant and hybrid flows** — tick **ID tokens**.
   `POST /api/auth/entra` validates an `id_token`, so without this the mobile
   and desktop sign-in flows return nothing to send.
4. Copy the Application (client) id and Directory (tenant) id into:
   - server: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`
   - clients: `EXPO_PUBLIC_ENTRA_CLIENT_ID`, `EXPO_PUBLIC_ENTRA_TENANT_ID`
   - Bicep: `NESQBOT_ENTRA_CLIENT_ID` (read by both `.bicepparam` files)

CLI equivalent:

```bash
az ad app create \
  --display-name "Nesq Bot" \
  --sign-in-audience AzureADMyOrg \
  --public-client-redirect-uris "nesqbot://auth" \
  --enable-id-token-issuance true \
  --query appId -o tsv
```

> **`AZURE_CLIENT_ID` is overloaded.** Once `entraClientId` is set it becomes
> the Entra app id, which is what token validation needs and what
> `ManagedIdentityCredential` must never be handed. The template therefore also
> sets `AZURE_MANAGED_IDENTITY_CLIENT_ID` on both apps, always to the
> user-assigned identity's client id. Anything making ARM calls — the ACI
> desktop driver above all — should read that one.

## Manual step: the EAS project id

Push notifications for approvals need an Expo Application Services project id
in `apps/mobile/app.json` under `expo.extra.eas.projectId`. Without it
`getExpoPushTokenAsync` cannot mint a token, `POST /api/me/devices` never gets
called, and approval notifications fail silently rather than erroring.

```bash
cd apps/mobile
eas login
npx eas init          # writes expo.extra.eas.projectId into app.json
```

Verify: `node -p "require('./apps/mobile/app.json').expo.extra.eas.projectId"`.

## Streaming: do not put a buffering proxy in front of the API

`POST /api/threads/{id}/messages/stream` and `GET /api/threads/{id}/events` are
Server-Sent Events. Container Apps ingress passes them through unbuffered, and
the compose stack publishes the API port directly with no proxy in between — so
this works out of the box. If you later add Front Door, App Gateway or nginx,
disable response buffering on those routes or every token in a turn arrives in
one lump at the end.

## Deliberate placeholders

These are known gaps, not oversights:

1. **Postgres, Redis, Key Vault and Storage are still public-access** with
   permissive firewalls, and Postgres uses the "allow all Azure services"
   pseudo-rule. The VNet is now in place, but Container Apps Consumption egress
   still leaves through a Microsoft-managed address unless you attach a NAT
   Gateway with a static IP — which costs roughly a third of the lean monthly
   bill on its own. The proper fix is private endpoints for Redis, Key Vault and
   Storage, and either a NAT Gateway or VNet-injected Postgres. VNet-injected
   Postgres would also cut off `psql` from a workstation, which the bootstrap
   above depends on, so it needs a jump path first.
2. **Container App secrets come from secure params, not Key Vault refs.** A
   `keyVaultUrl` secret reference is resolved when the app is created, which
   would fail on a first deploy into an empty vault. The same values are also
   written to Key Vault, so rotation has one home; switching the apps to
   `keyVaultUrl` refs is a one-line change per secret once the vault is seeded.
3. **No custom domain or certificate** on the API ingress; it answers on the
   generated `*.azurecontainerapps.io` FQDN. `apiAllowedOrigins` already names
   the intended hostnames.
4. **`allowSharedKeyAccess: true`** on the storage account, because Azure Files
   SMB mounts on Container Apps still need the account key. Blob access already
   goes through the managed identity.
5. **No alerts or dashboards.** Log Analytics and App Insights are wired up and
   collecting, but no action groups, metric alerts or workbooks are defined. A
   Temporal replica that will not start is currently something you notice by
   looking.
6. **No Temporal dynamic config.** The compose stack mounts
   `infra/temporal/dynamicconfig/development-sql.yaml`; the Container App uses
   the image defaults. Mounting a file would mean another Azure Files share for
   two lines of YAML.
7. **Redis has no HA on the lean tier.** One container, one replica, no
   persistence. A restart drops in-flight SSE fanout; clients reconnect and the
   in-process fallback covers a single replica in the meantime. `sizingTier='full'`
   is the answer if that stops being acceptable.
8. **All desktops share one Azure Files share.** See the note under "Bot homes
   must be a volume" above — `AzureFileVolume` has no sub-path option, so
   per-bot home isolation needs per-bot shares.
9. **AKS is off** and the path is unmaintained. `infra/bot-desktop/k8s/desktop-template.yaml`
   is still there but nothing applies it.

## Cost

Rough Sweden Central list prices, USD per month, before any token spend.
Container Apps Consumption bills per vCPU-second, so a replica floor is a
standing charge and the app tier — not the databases — is the biggest line.

| Item                              | lean                        | full                        |
| --------------------------------- | --------------------------- | --------------------------- |
| Postgres Flexible Server          | B2s Burstable, 32 GB — ~$34 | D2ds_v5 GP + ZR HA, 128 GB — ~$390 |
| Redis                             | Container App 0.25 vCPU — ~$6–20 | Managed Redis Balanced_B1 + HA — ~$90 |
| Container App `api`               | 0.5 vCPU / 1 GiB, min 1 — ~$12 idle, ~$40 busy | 1 vCPU / 2 GiB, min 2 — ~$140 |
| Container App `worker`            | 0.5 vCPU / 1 GiB, min 1 — ~$20–40 | 1 vCPU / 2 GiB — ~$70  |
| Container App `temporal`          | 0.5 vCPU / 1 GiB, min 1 — ~$40–47 | 1 vCPU / 2 GiB — ~$79  |
| NAT gateway + static public IP    | ~$37 + $0.045/GB            | ~$37 + $0.045/GB            |
| ACR                               | Basic — $5                  | Standard — $20              |
| Storage, Key Vault, Log Analytics | ~$15                        | ~$30                        |
| VNet, NSG, subnets                | $0                          | $0                          |
| Bot Desktops (ACI)                | ~$0.10 per desktop-hour, per second, only while running | same |
| **Total**                         | **~$170–235**               | **~$850+**                  |

Four honest notes on the lean figure:

- **It lands above the $80–110 target, and self-hosted Temporal is why.** An
  always-on 0.5 vCPU Container App is $40–47/month at Consumption rates, and it
  cannot scale to zero — a workflow server that is not running is not a workflow
  server. Temporal Cloud would be cheaper at this volume and would also ship
  every workflow input and output to a US company, which is the trade the
  residency story exists to refuse.
- **The active/idle spread is real.** Container Apps bills idle replicas at
  roughly a eighth of the active vCPU rate, and only counts a replica idle when
  it is serving no requests and using under 0.01 vCPU. The API spends most of
  its time there; Temporal's poll loops mostly do not. The ranges above are the
  two ends of that.

- **Redis is cheaper than before, and weaker than before.** At 0.25 vCPU the
  container costs roughly $6/month idle against $16 for the Basic C0 it
  replaces. That is a real saving and a real downgrade in durability and
  operability; it only works because this workload has neither requirement.
  Managed Redis at ~$55/month remains one parameter away if that changes.
- **The NAT gateway is not optional and it is not cheap.** ~$37/month for a
  component whose only job is giving the desktop subnet a route out. It is the
  price of VNet-injected ACI after the default-outbound retirement; the
  alternatives are a Premium ACR for a private endpoint (more) or desktops with
  no internet (not a product).

The cheapest genuine levers, if the bill has to come down, are consolidating the
worker into the API process — one fewer always-on replica floor, about $30/month
— and setting `deployAciNatGateway=false` while `botDesktopMode` is still
`mock`. Not shrinking the databases, which are already the small numbers.
