using 'main.bicep'

// =============================================================================
// Nesq Bot - dev environment parameters
// =============================================================================
// Production lives in prod.bicepparam. This file is the sandbox.
//
// Secrets are read from the environment, never written here. Set them before
// deploying (bash / Git Bash):
//
//   export NESQBOT_PG_PASSWORD="$(openssl rand -base64 32)"
//   export NESQBOT_JWT_SECRET="$(openssl rand -base64 48)"
//   export NESQBOT_WORKER_TOKEN="$(openssl rand -base64 48)"
//   export NESQBOT_SIDECAR_TOKEN="$(openssl rand -base64 32)"
//
//   az deployment group create -g rg-nesqbot-dev \
//     -f infra/azure/main.bicep -p infra/azure/main.bicepparam
//
// PowerShell:
//   $env:NESQBOT_PG_PASSWORD = [Convert]::ToBase64String((1..32 | % { Get-Random -Max 256 }))
//
// `az bicep build-params` fails fast if any of these are unset, which is the
// intended behaviour - a missing secret must never fall back to a default.
// =============================================================================

param environmentName = 'dev'
param sizingTier = 'lean'
param location = 'swedencentral'

param postgresAdminLogin = 'pgadmin'
param postgresAdminPassword = readEnvironmentVariable('NESQBOT_PG_PASSWORD')
param jwtSecret = readEnvironmentVariable('NESQBOT_JWT_SECRET')
param workerApiToken = readEnvironmentVariable('NESQBOT_WORKER_TOKEN')
param sidecarToken = readEnvironmentVariable('NESQBOT_SIDECAR_TOKEN')

// Entra: defaults to the deploying tenant. Set the client id once the app
// registration for desktop/mobile sign-in exists.
param entraClientId = readEnvironmentVariable('NESQBOT_ENTRA_CLIENT_ID', '')

// Temporal runs as an internal-ingress Container App against its own databases
// on the Postgres server this template creates. Set NESQBOT_TEMPORAL_HOST only
// to point somewhere else instead - Temporal Cloud, or a shared cluster.
param deployTemporal = true
param temporalHost = readEnvironmentVariable('NESQBOT_TEMPORAL_HOST', '')
param temporalNamespace = 'default'

// First deploy: leave deployApps = false so the Container Apps are not created
// against an empty ACR, push the images, then flip it to true and redeploy.
param deployApps = bool(readEnvironmentVariable('NESQBOT_DEPLOY_APPS', 'true'))
param imageTag = readEnvironmentVariable('NESQBOT_IMAGE_TAG', 'v0.1.0')

// mock in dev: the VNet and the delegated subnet still get created, so
// flipping this to 'aci' is a redeploy and nothing else.
param botDesktopMode = readEnvironmentVariable('NESQBOT_BOT_DESKTOP_MODE', 'mock')

// Off in dev: it is ~USD 37/month for egress that mock desktops never use.
// Flip it to true in the same change that flips botDesktopMode to 'aci', or
// the first ACR pull will hang until the container group start timeout.
param deployAciNatGateway = false

// AKS stays off. ACI does the same job per second instead of per node-hour.
param deployBotDesktopAks = false
param botDesktopNodeCount = 1
param botDesktopNodeSize = 'Standard_D4as_v5'

param tags = {
  costCenter: 'internal-tools'
  owner: 'nesqualtech'
}
