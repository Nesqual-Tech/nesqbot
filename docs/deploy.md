# Deployment

Production runs on Azure. The whole topology is one Bicep template:
**`infra/azure/main.bicep`**, parameterised by
`infra/azure/main.bicepparam`, documented in `infra/azure/README.md`. That
template is the source of truth for what exists in a given environment; this
page is the map and the checklist.

## Topology

| Component      | Azure resource                          | Notes                                                                                                                 |
| -------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| API            | Container App                           | 1–3 replicas in dev, 2–10 in prod. External ingress                                                                   |
| Worker         | Container App                           | 1–2 replicas in dev, 1–5 in prod. No ingress                                                                          |
| Database       | PostgreSQL Flexible Server + `pgvector` | Burstable B2s in dev, D2ds_v5 in prod. 7-day backups in dev, 35 in prod                                               |
| Cache / pubsub | Azure Cache for Redis                   | Basic C0 in dev, Standard C1 in prod                                                                                  |
| Models         | Azure OpenAI / AI Foundry               | `gpt-4.1-nano`, `gpt-4.1-mini`, `o4-mini`, `text-embedding-3-small`                                                   |
| Secrets        | Key Vault                               | `jwt-secret`, `worker-api-token`, `sidecar-token`, `postgres-admin-password`, `database-url`                          |
| Images         | Azure Container Registry                | API, worker, bot-desktop                                                                                              |
| Storage        | Storage Account                         | Azure Files share for bot homes; blob containers for artifacts and screenshots                                        |
| Bot Desktop    | AKS node pool                           | Off by default (`deployBotDesktopAks = false`) — the most expensive resource in the template                          |
| Telemetry      | Log Analytics + App Insights            | 30-day retention in dev, 90 in prod                                                                                   |
| Identity       | User-assigned managed identity          | ACR pull, Key Vault secrets user, Storage blob/file contributor, Cognitive Services OpenAI user, monitoring publisher |

Temporal is **not** provisioned by the template. Point `TEMPORAL_HOST` at
Temporal Cloud (`namespace.account.tmprl.cloud:7233`) or a self-hosted cluster.
With it unset, routines fall back to inline execution.

## Deploying

Secrets come from the environment, never from the parameter file:

```bash
export NESQBOT_PG_PASSWORD="$(openssl rand -base64 32)"
export NESQBOT_JWT_SECRET="$(openssl rand -base64 48)"
export NESQBOT_WORKER_TOKEN="$(openssl rand -base64 48)"
export NESQBOT_SIDECAR_TOKEN="$(openssl rand -base64 32)"
export NESQBOT_ENTRA_CLIENT_ID="…"        # once the app registration exists

az group create -n rg-nesqbot-dev -l swedencentral

# First pass: infrastructure only, because ACR is still empty
NESQBOT_DEPLOY_APPS=false az deployment group create \
  -g rg-nesqbot-dev -f infra/azure/main.bicep -p infra/azure/main.bicepparam

# Push images to the ACR the template just created
az acr login -n "$(az deployment group show -g rg-nesqbot-dev -n main \
  --query properties.outputs.acrName.value -o tsv)"
docker build -t "$ACR/nesqbot/api:v0.1.0"    apps/api
docker build -t "$ACR/nesqbot/worker:v0.1.0" apps/worker
docker push "$ACR/nesqbot/api:v0.1.0"
docker push "$ACR/nesqbot/worker:v0.1.0"

# Second pass: bring up the Container Apps
az deployment group create \
  -g rg-nesqbot-dev -f infra/azure/main.bicep -p infra/azure/main.bicepparam
```

Useful outputs: `apiUrl`, `acrLoginServer`, `keyVaultUri`,
`databaseUrlTemplate`, `redisUrlTemplate`, `botDesktopImage`, `envSummary`.

The API creates its own schema and seeds system bots on startup
(`ensure_schema` + `seed_system`). In `NESQ_ENV=production` a failure there is
fatal, so the container never passes its health check with a half-initialised
database.

## Environment variables

Set on the API unless noted. Anything marked **secret** must be a Container App
secret reference into Key Vault, never a literal.

### Core

| Variable                | Prod value         | Notes                                                           |
| ----------------------- | ------------------ | --------------------------------------------------------------- |
| `NESQ_ENV`              | `production`       | **Security control.** Anything else enables the dev auth bypass |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8080` |                                                                 |
| `DATABASE_URL`          | secret             | `postgresql+asyncpg://…?ssl=require`                            |
| `REDIS_URL`             | secret             | `rediss://:<key>@…:6380/0`                                      |
| `JWT_SECRET`            | secret             | Session signing key. Rotating it logs everyone out              |
| `CORS_ORIGINS`          | explicit list      | Empty falls back to `*` — always set it                         |

### Auth

| Variable                   | Notes                                          |
| -------------------------- | ---------------------------------------------- |
| `AZURE_TENANT_ID`          | Entra tenant for JWKS and issuer validation    |
| `AZURE_CLIENT_ID`          | App registration; validated as the token `aud` |
| `AZURE_CLIENT_SECRET`      | secret, only if a confidential flow is added   |
| `ENTRA_JWKS_CACHE_SECONDS` | Default 3600                                   |

### Models

| Variable                                                 | Notes                                           |
| -------------------------------------------------------- | ----------------------------------------------- |
| `AZURE_OPENAI_ENDPOINT`                                  | From the `openaiEndpoint` output                |
| `AZURE_OPENAI_API_KEY`                                   | secret. Prefer managed identity where supported |
| `AZURE_OPENAI_API_VERSION`                               | `2024-12-01-preview`                            |
| `AZURE_DEPLOYMENT_NANO` / `_MINI` / `_REASON` / `_EMBED` | Deployment names, not model names               |
| `DEFAULT_BOT_DAILY_BUDGET_USD`                           | Soft cap for new bots                           |

Unset endpoint or key ⇒ the model router serves mock replies. Never ship that.

### Execution

| Variable                                                       | Notes                                                                               |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `BOT_DESKTOP_MODE`                                             | `mock` \| `docker` \| `aks`. `aks` in production, `mock` until the node pool exists |
| `BOT_DESKTOP_IMAGE`                                            | From the `botDesktopImage` output                                                   |
| `BOT_DESKTOP_HOME_ROOT`                                        | Azure Files mount path                                                              |
| `BOT_DESKTOP_STREAM_BASE`                                      | KasmVNC base URL                                                                    |
| `NESQ_SIDECAR_TOKEN`                                           | secret. Shared with the desktop sidecar                                             |
| `TEMPORAL_HOST` / `TEMPORAL_NAMESPACE` / `TEMPORAL_TASK_QUEUE` | Unset ⇒ inline routine fallback                                                     |
| `WORKER_API_TOKEN`                                             | secret. Worker → API service credential                                             |
| `API_INTERNAL_URL`                                             | Worker only. In-environment API URL                                                 |

### Platform

| Variable                                | Notes                                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------------ |
| `AZURE_KEY_VAULT_URL`                   | From `keyVaultUri`. Connector `secret_ref` resolution (see the gap in `security.md`) |
| `BOTS_DIR`                              | Mount path for `bots/` YAML                                                          |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Telemetry                                                                            |
| `EXPO_PUSH_ENABLED`                     | Approval push notifications to mobile                                                |

## Production checklist

Before pointing this at real customer data:

**Configuration**

- [ ] `NESQ_ENV=production` on every app. Verify `POST /api/auth/dev-login` returns 403.
- [ ] `CORS_ORIGINS` set explicitly. Confirm a random origin is rejected.
- [ ] `JWT_SECRET` generated per environment and stored in Key Vault. Not `dev-change-me`.
- [ ] Every secret is a Key Vault reference; `az containerapp show` reveals no literals.

**Identity and access**

- [ ] Entra app registration exists; `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` set.
- [ ] Sign-in tested end to end from desktop and mobile.
- [ ] Managed identity role assignments verified (ACR pull, Key Vault, Storage, OpenAI).
- [ ] Read `security.md` → _Known gaps_, and accept or fix each one. In particular: any authenticated user can currently decide any system bot's approvals.

**Data**

- [ ] `pgvector` present. Postgres firewall restricted to the Container Apps subnet.
- [ ] Backups on with the retention you actually want; a restore rehearsed once.
- [ ] Redis TLS-only (`rediss://`, port 6380).

**Models and budget**

- [ ] Foundry deployments exist with the exact names in `AZURE_DEPLOYMENT_*`.
- [ ] A test turn returns a real reply, not `[mock:mini]`.
- [ ] Per-bot `daily_budget_usd` reviewed. Remember it is a soft cap.
- [ ] A cost alert on the Azure OpenAI resource, not just the in-app ledger.

**Execution**

- [ ] `BOT_DESKTOP_MODE` matches reality — `aks` only once the node pool is up.
- [ ] Bot homes on the Azure Files share, and they survive a pod restart.
- [ ] Temporal reachable, or the inline fallback accepted for routines.
- [ ] `WORKER_API_TOKEN` and `NESQ_SIDECAR_TOKEN` rotated off their bootstrap values.

**Operations**

- [ ] `GET /api/health/deep` green for db, redis and temporal.
- [ ] App Insights receiving traces; `X-Request-Id` visible in logs.
- [ ] Alerts on API 5xx rate, worker restarts, and Postgres connection saturation.
- [ ] Image tags pinned — no `:latest` in any Container App.
- [ ] Rollback rehearsed: redeploy the previous `imageTag` and confirm.

## Environments

`environmentName` drives sizing (`dev` vs `prod`): SKUs, replica counts,
backup retention and log retention all key off it. Deploy each environment
into its own resource group with its own parameter file. `dev` deliberately
leaves AKS off and expects `BOT_DESKTOP_MODE=mock`.
