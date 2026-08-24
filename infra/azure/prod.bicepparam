using 'main.bicep'

// =============================================================================
// Nesq Bot - production parameters ("lean prod")
// =============================================================================
// environmentName = 'prod'   full production hardening
// sizingTier      = 'lean'   burstable hardware, roughly a tenth of the spend
//
// Those are two separate parameters on purpose. Production behaviour - no
// dev-login, no localhost CORS, purge-protected Key Vault, 90 day logs, 35 day
// backups - costs nothing. Zone-redundant Postgres and a Standard Redis cost a
// great deal. An internal team needs the first and not the second.
//
// Secrets come from the environment and have no defaults anywhere in the
// template, so a missing one fails at build-params time rather than deploying
// a placeholder:
//
//   $env:NESQBOT_PG_PASSWORD    = python -c "import secrets;print(secrets.token_urlsafe(32))"
//   $env:NESQBOT_JWT_SECRET     = python -c "import secrets;print(secrets.token_urlsafe(48))"
//   $env:NESQBOT_WORKER_TOKEN   = python -c "import secrets;print(secrets.token_urlsafe(48))"
//   $env:NESQBOT_SIDECAR_TOKEN  = python -c "import secrets;print(secrets.token_urlsafe(32))"
//
// All four also land in Key Vault, which is where they should be read from
// afterwards. Rotate them there, not here.
// =============================================================================

param environmentName = 'prod'
param sizingTier = 'lean'
param location = 'swedencentral'

param postgresAdminLogin = 'pgadmin'
param postgresAdminPassword = readEnvironmentVariable('NESQBOT_PG_PASSWORD')
param jwtSecret = readEnvironmentVariable('NESQBOT_JWT_SECRET')
param workerApiToken = readEnvironmentVariable('NESQBOT_WORKER_TOKEN')
param sidecarToken = readEnvironmentVariable('NESQBOT_SIDECAR_TOKEN')

// Your own two app registrations, see docs/entra-setup.md. They are NOT
// interchangeable:
//   entraApiAppId  "Nesq Bot API"  - the audience the API accepts
//   entraClientId  "Nesq Bot"      - the public desktop/mobile client
// Both are issued by the same tenant and verify against the same JWKS, so
// swapping them makes the API accept tokens minted for the wrong audience,
// silently. No defaults here on purpose - a missing one fails at
// build-params time rather than deploying a placeholder that half works.
param entraApiAppId = readEnvironmentVariable('NESQBOT_ENTRA_API_APP_ID')
param entraClientId = readEnvironmentVariable('NESQBOT_ENTRA_CLIENT_ID')
param entraApiScope = 'access_as_user'

// Self-hosted Temporal, internal ingress, its own databases on the Postgres
// server below. Workflow history stays in the tenant and in the EU, which is
// the entire reason for not using Temporal Cloud.
// Redis: Azure Cache for Redis is retired, and so is the Enterprise_E* family
// that replaced it. Azure Managed Redis (Balanced_B*) is available here from
// ~USD 55/mo, but the lean tier runs Redis as a Container App at ~USD 6-20
// instead - it is pub/sub fanout only, with no persistence, and nothing
// durable may live in it. sizingTier='full' switches to Managed Redis with HA.
// Pinned image, matching docker-compose.yml.
param redisImage = 'redis:7.4-alpine'

param deployTemporal = true
param temporalHost = ''
param temporalNamespace = 'default'
param temporalNamespaceRetention = '720h'

// Bot Desktops on Azure Container Instances: one hypervisor-isolated container
// group per bot, private IP on the delegated subnet, billed per second.
param botDesktopMode = 'aci'
param aciCpu = 2
param aciMemoryGb = 4

// Not optional with botDesktopMode='aci'. A delegated ACI subnet has had no
// default outbound internet since Azure retired default outbound access on
// 2025-09-30, so without this the ACR pull hangs until the start timeout
// rather than failing, and a desktop with no internet cannot sign into
// anything either. ~USD 37/month.
param deployAciNatGateway = true

// FIRST DEPLOY: leave this false. ACR is empty, so an api/worker Container App
// would be created against an image that does not exist and sit in
// ContainerCreating forever. Everything else - VNet, Postgres, Redis, Key
// Vault, ACR, Foundry deployments, RBAC, Temporal - comes up on this pass.
// Push the images, then set NESQBOT_DEPLOY_APPS=true and run the same command.
// Default flipped to true on 2026-08-22, once the images were in ACR and the
// api/worker/redis apps were live. It defaulted to false for the very first
// deploy, when an empty registry meant a Container App would sit forever in
// ContainerCreating. That default is now actively dangerous: a deploy run
// without NESQBOT_DEPLOY_APPS set would *delete* the running API and worker,
// and forgetting an env var should not be able to take production down.
// Set NESQBOT_DEPLOY_APPS=false deliberately to stand up a data plane alone.
param deployApps = bool(readEnvironmentVariable('NESQBOT_DEPLOY_APPS', 'true'))
// v0.1.1 adds managed-identity auth to the model router. v0.1.0 authenticated
// to Azure AI Foundry with an API key only, and production has no key by
// design, so the router fell through to its mock branch and every bot replied
// with canned text. Leaving this pinned at v0.1.0 would silently roll that
// regression back on the next deploy, which is worse than the original bug
// because the app would look deployed and behave fake.
// v0.1.0 is still in ACR as a rollback target.
// Baseline tag for anything without its own override.
param imageTag = readEnvironmentVariable('NESQBOT_IMAGE_TAG', 'v0.1.0')

// The three images move independently, and pinning them to one tag was a live
// hazard: production ran api:v0.1.3 against worker:v0.1.0, so any redeploy was
// going to move one of them. These match what is actually deployed today.
// v0.1.3 = managed-identity Foundry auth + the ACI credential fix; anything
// older makes the API mock every model call.
// v0.2.0 is the autonomous agent loop: native tool calling, bot-managed
// desktop lifecycle, and the awaiting_human takeover/resume state machine.
// v0.2.1 makes the agent loop affordable: screenshots are pruned from the
// conversation before every model call (they used to accumulate, so a 35-step
// run re-sent 630 images and cost ~$4 of a $5 budget), plus JPEG/downscale and
// reasoning_effort="none" on the step calls. Measured 5.5x cheaper, 41% faster.
param apiImageTag = readEnvironmentVariable('NESQBOT_API_IMAGE_TAG', 'v0.7.0')
param workerImageTag = readEnvironmentVariable('NESQBOT_WORKER_IMAGE_TAG', 'v0.1.0')
// v0.1.1 drops the desktop to 1280x800x24. Measured against the app's pane: a
// maximised view renders ~97% on a 1920x1080 screen instead of 86%, and each
// screenshot carries ~11% fewer pixels - which is a direct per-step saving in
// the agent's vision loop, where every action costs an image.
// v0.2.0 adds the CDP browser API on the sidecar (/browser/*): the same
// Chromium, driven by element reference instead of by pixel coordinate. The
// API's browser_* tools need it - against a v0.1.1 desktop every one of them
// answers 503 browser_unavailable and the agent falls back to pixels, which is
// safe but is the behaviour this tag exists to stop.
param botDesktopImageTag = readEnvironmentVariable('NESQBOT_BOT_DESKTOP_IMAGE_TAG', 'v0.2.4')

// No AKS. ACI covers per-bot desktops without a node floor.
param deployBotDesktopAks = false

// The Tauri desktop webview is the real client today and its origin is
// tauri.localhost - not a loopback dev server, it is the scheme the packaged
// app serves itself from on Windows. Without it the installed app is blocked
// by CORS on every request. Native mobile is exempt from CORS entirely.
// The two nesqualtech hostnames are placeholders for a future web client and
// resolve to nothing yet; they are harmless but not load-bearing.
param apiAllowedOrigins = [
  'http://tauri.localhost'
  'https://tauri.localhost'
  'https://app.example.com'
  'https://mobile.example.com'
]

// 10.60.0.0/16 was picked to stay clear of the usual 10.0/10.1 corporate
// ranges. Change it before deploying if it collides with anything peered.
param vnetAddressPrefix = '10.60.0.0/16'
param containerAppsSubnetPrefix = '10.60.0.0/23'
param aciSubnetPrefix = '10.60.4.0/23'

param tags = {
  costCenter: 'CHANGE_ME'
  owner: 'CHANGE_ME'
  dataResidency: 'eu-sweden'
}
