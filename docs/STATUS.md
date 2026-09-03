# Status

What is actually built, what is faked, and what is missing. Updated with the
workspace conversion. "Mocked" means the code path exists and returns a
plausible answer without the real dependency — useful for development, never
acceptable in production.

Legend: **done** · **mocked** · **partial** · **missing**

## Control plane (`apps/api`)

| Area                        | State       | Notes                                                                                                                                                                                                                                 |
| --------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Route surface vs `API.md`   | **done**    | Every endpoint in the contract is mounted, including runs, audit, memories, KB, usage, evals, device registration and both SSE endpoints                                                                                              |
| Error envelope              | **done**    | `{detail, code}`, 500s carry `request_id` from the `X-Request-Id` middleware                                                                                                                                                          |
| Dev auth bypass             | **done**    | `NESQ_ENV=development` + missing `Authorization` or `X-Nesq-Dev: 1`                                                                                                                                                                   |
| Entra sign-in               | **done**    | Real JWKS fetch with cache and rollover, `aud`/`iss` validated, upsert by `oid`                                                                                                                                                       |
| Session tokens              | **partial** | HS256, 14 days. `POST /auth/logout` revokes the presented token (`jti` in `revoked_tokens`, checked on every request, pruned at boot). No refresh endpoint — a token cannot be renewed short of signing in again              |
| Ownership / visibility      | **done**    | System bots shared, custom bots and threads owned; invisible objects 404 rather than 403                                                                                                                                              |
| Roles / RBAC                | **missing** | Every authenticated user is equal. See `security.md`                                                                                                                                                                                  |
| Schema + seeding on boot    | **done**    | `ensure_schema` + `seed_system`; fatal in production, tolerated in dev                                                                                                                                                                |
| Streaming turns             | **done**    | SSE `token`/`handoff`/`tool`/`approval`/`done`/`error`                                                                                                                                                                                |
| Risk gate + approvals       | **done**    | Connector, MCP and desktop actions all gated; execution replays the stored payload server-side                                                                                                                                        |
| Approval scoping            | **done**    | Owner-resolved (`requested_by` payload → thread owner → custom bot owner), decide/expire require caller to be that owner; unattributable approvals fall back to bot visibility. 404 not 403. See `routers/deps.py::get_visible_approval` |
| Key Vault secret resolution | **done when configured** | `app/services/secrets.py` resolves `kv://`, `env://` and bare refs with caching and a no-logging contract. `_invoke_vendor` dispatches to real drivers: `vendors/graph.py` (Microsoft Graph mail, live once `GRAPH_API_BASE_URL` set) and `vendors/generic_http.py` (manifest-driven, live once a connector's manifest sets `base_url`). No connector/vault exercised against real credentials yet |
| Rate limiting               | **missing** | Only the per-bot daily budget, which is a soft cap                                                                                                                                                                                    |

## Models and RAG

| Area                  | State                    | Notes                                                                                         |
| --------------------- | ------------------------ | --------------------------------------------------------------------------------------------- |
| Tier routing          | **done**                 | `nano`/`mini`/`reason`/`embed` by task class, mirrored in `@nesqbot/model-router`             |
| Multi-provider models | **done when configured** | `MODEL_PROVIDER` (global + per-tier) picks `azure` (default, unchanged), `openai` (real OpenAI or any self-hosted OpenAI-compatible server — "local models": Ollama, vLLM, LM Studio, OpenRouter — via `OPENAI_BASE_URL`), `anthropic` (real Claude, request/response translated to Anthropic's Messages API wire format in `services/model_router.py`, verified against the installed SDK's real types — no live account exercised; `reasoning_effort` maps to extended thinking's `budget_tokens`, and a forced `tool_choice` is dropped when thinking turns on since Anthropic forbids that combination), or `google` (real Gemini, built against `google-genai`'s `generate_content`/`generate_content_stream` — the mature, strongly-typed entry point, not the SDK's newer loosely-typed `interactions.create` surface — verified against the installed SDK's real types, no live account exercised; `reasoning_effort` maps directly onto Gemini's own `ThinkingLevel` enum, which happens to use the identical `minimal`/`low`/`medium`/`high` vocabulary, so unlike Anthropic there is no token-budget guess involved). `bots.model_provider`/`model_name` let one bot pin itself to a provider/model, bypassing tier routing (`ModelRouter.chat(bot=...)`, wired into every orchestrator call site that represents a bot's own conversational turn). `MODEL_PRICES` bills a pinned bot at its real model's price (fetched live from openai.com/api/pricing, platform.claude.com and ai.google.dev/gemini-api/docs/pricing on 2026-08-26 — the Gemini entries are promotional rates through 2026-12-31) when the model is listed there, instead of guessing off the task's Azure tier — an unlisted model still gets the loud tier-based warning. `GET /bots/providers` reports which are actually live; the desktop app's setup wizard (`SetupWizard.tsx`, reachable again from the command palette as "Open setup") reads it to offer only working providers, and can point the app at any self-hosted backend at runtime (`api/client.ts`'s `setApiBase`, no rebuild needed — desktop only had a build-time `VITE_API_URL` before this). `BuilderPanel.tsx`'s edit form exposes the same per-bot override. Mobile (`app/(tabs)/bots.tsx`) gets a compact per-bot picker too — a collapsed status line that expands to the same provider chips + model text field, deliberately not a general edit form (this file's own header comment reserves authoring for the desktop; a provider/model pin sits with the budget field as an operational setting, not authoring). A provider key no longer has to live in the backend's own environment: `provider_credentials.py` adds an app-writable layer on top — encrypted in Postgres (Fernet, key derived from `JWT_SECRET`, so a rotation invalidates every stored key), additive only (an operator's env var for the same provider always wins), reachable from the Builder's "Add a provider API key" section next to the per-bot model picker (`GET /bots/providers/credentials`, `POST`/`DELETE /bots/providers/{provider}/credential` — `POST`, not `PUT`: the Container Apps ingress `corsPolicy` in `infra/azure/main.bicep` predates this endpoint and does not allow `PUT`, a separate edge-level CORS check ahead of the app's own `CORSMiddleware`), and picked up by `model_router.py`'s config resolution with zero threading of a DB session through the router — one in-memory override table, refreshed on write and on a 5-minute TTL for other replicas. The Builder's model field is a live dropdown, not free text (`GET /bots/providers/{provider}/models`, `ModelRouter.list_models`) — Azure through the account's real `/deployments` (not `.models.list()`, which is the base-model catalog Azure offers to deploy, not what is actually deployed), the other three through their own SDK's `.models.list()`; falls back to typing a name by hand when an account cannot be listed (a self-hosted OpenAI-compatible server with no `/models`, a scoped key). Azure specifically merges deployments across every distinct account this deployment knows about, not just the shared one — the `reason` tier's endpoint override (`AZURE_OPENAI_ENDPOINT_REASON`) points at a second Foundry resource this production deployment actually uses for Grok (`grok-4-1-fast-reasoning`, `grok-4.3` — invisible to the shared account's own `/deployments`), and `_client_for` now matches a pinned bot's `model_name` against every tier's configured deployment name to route the request to the account that actually has it, rather than 404ing against the shared one |
| Azure Foundry calls   | **done when configured** | Retries via tenacity, 60 s timeout                                                            |
| Model fallback        | **mocked**               | No endpoint/key ⇒ deterministic `[mock:<tier>]` replies, still ledgered with estimated tokens |
| Cost ledger + budgets | **done**                 | One row per completion; soft stop at the daily cap                                            |
| Embeddings            | **mocked**               | No Azure ⇒ `embed()` returns `None`                                                           |
| Memory / KB search    | **partial**              | pgvector when embeddings exist, keyword `ILIKE` fallback otherwise                            |

## Execution

| Area                     | State                               | Notes                                                                           |
| ------------------------ | ----------------------------------- | ------------------------------------------------------------------------------- |
| Bot Desktop lifecycle    | **done**                            | start / stop / suspend / resume / action / screenshot / windows                 |
| Desktop backend `mock`   | **mocked**                          | Default. State transitions plus a generated placeholder PNG                     |
| Desktop backend `docker` | **done**                            | Container per bot, home bind-mounted from `data/bot-homes/`                     |
| Desktop backend `aks`    | **missing**                         | `services/desktop.py` sets `state="starting"` and a placeholder `container_id`, then stops — `apps/worker` has zero aks/pod/kubernetes reconciliation code, so it can never leave `starting` |
| Temporal worker          | **done**                            | Workflows, activities, health file, graceful drain                              |
| Routine schedules        | **done when Temporal is reachable** | Sync on create/patch, delete on delete                                          |
| Routine inline fallback  | **partial**                         | No Temporal ⇒ `services/routines.py` runs steps inline through the same `simulation.perform`/risk-gate/approval path as the Temporal-backed run — real effects, not canned output. Only gap: no real `workflow_id` for tracking |
| Redis pubsub             | **done**                            | In-process fallback when Redis is absent, single replica only                   |
| Mobile push on approval  | **partial**                         | Device registration and Expo push are wired; not verified against a real device |

## Clients

| Area                    | State       | Notes                                                                                                  |
| ----------------------- | ----------- | ------------------------------------------------------------------------------------------------------ |
| Desktop (Tauri + React) | **partial** | Bots, chat, approvals, integrations, usage, audit, knowledge, builder, setup wizard. Tauri shell builds; CI builds the web bundle only. `GET /audit`, `/kb`, and per-bot `/memories` had no UI consumer until `AuditPanel.tsx`/`KnowledgePanel.tsx`/`BuilderPanel.tsx`'s `MemoriesSection` (memories live in the bot editor, not their own tab — they are scoped to one bot and have nothing to say without one already selected). `/work-items` and `/work-items/{id}/transfers` are rendered by `WorkPanel.tsx`; progress is the API's own status counts, never an invented percentage. The shell is a conversation list plus a settings sheet — the nine-tab rail is gone, and everything except chat now lives behind the settings button (`SettingsSheet.tsx`) |
| Mobile (Expo)           | **partial** | Chat, approvals, live desktop view                                                                     |
| Design system           | **done**    | Tokens, semantic risk and bot-state roles, elevation, type scale, motion with a reduced-motion variant |
| Shared protocol types   | **done**    | Full contract surface, approval payload union, SSE union, runtime guards                               |

## Monorepo and tooling

| Area                        | State       | Notes                                                                                                                                                                                                                         |
| --------------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| npm workspaces              | **done**    | `apps/*` + `packages/*`, one root install                                                                                                                                                                                     |
| Root scripts                | **done**    | `dev`, `desktop`, `mobile`, `api`, `worker`, `build`, `typecheck`, `lint`, `format`, `clean`                                                                                                                                  |
| `tsconfig.base.json`        | **done**    | Strict, ES2022, bundler resolution, composite project references                                                                                                                                                              |
| Source-only package exports | **done**    | Deliberate — documented in every `package.json` and in `local-dev.md`                                                                                                                                                         |
| CI                          | **partial** | GitHub Actions: ruff, mypy, pytest, Bicep build/lint/what-if, Node typecheck, desktop bundle, image builds. `macos.yml` also does full native Tauri `.app`/`.dmg` builds with codesign/notarize verification                |
| Python tests                | **done**    | `apps/api/tests` has 74 files (services + router-level: risk gating, approval scoping, auth, contract coverage). `apps/worker/tests` has one. Run status not re-verified locally as of this edit                            |
| API/protocol parity check   | **done**    | `npm run check:api --workspace @nesqbot/protocol` diffs the TS interfaces against `schemas.py` for field presence and nullability. Advisory, not wired into CI                                                                |
| TypeScript tests            | **missing** | No test runner configured in any package                                                                                                                                                                                      |
| Prettier                    | **done**    | Configured in the root `package.json` (no semicolons, 120 cols, matching the existing style); `.prettierignore` covers lockfiles, generated output and `docs/API.md`. The tree has been formatted, so `npm run lint` is green |
| Dependency scanning         | **partial** | `dependency-scan`/`node-dependency-scan` jobs in `ci.yml` run `pip-audit`/`npm audit`, advisory (`continue-on-error`) until a first pass is triaged. `.github/dependabot.yml` opens weekly update PRs for pip/npm/actions   |

## Infrastructure

| Area                 | State       | Notes                                                                                                                                                          |
| -------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Azure Bicep          | **done**    | Full topology: ACA, Postgres+pgvector, Redis, Key Vault, ACR, Storage, OpenAI deployments, Log Analytics, App Insights, managed identity with role assignments |
| Self-hosted k8s Bot Desktop (`bot_desktop_mode=k8s`) | **done** | `app/services/desktop.py` drives any standard cluster (k3s, kind, EKS, GKE, bare metal) directly through the `kubernetes` Python client and a kubeconfig - no Azure dependency. One Pod + Service per bot, same env-var/port contract as `aci` (BOT_SLUG, DESKTOP_PROFILE, VNC_PW, NESQ_STREAM_PORT, NESQ_SIDECAR_PORT, NESQ_SIDECAR_TOKEN) so one image serves both. Real persistence unlike `aci`: a PersistentVolumeClaim when `k8s_storage_class` is set, or a hostPath directory (single-node/dev only) when it is not - a plain `stop` keeps the bot's home, only `wipe=true` deletes it. `suspend` deletes the Pod+Service (frees node CPU/mem) but keeps the volume, so `resume` is a cold start onto the same filesystem rather than a blank one - strictly better than `aci`, which loses everything on stop. Pod spec is hardened to the same posture as `infra/bot-desktop/k8s/desktop-template.yaml` (non-root, capabilities dropped, seccomp RuntimeDefault) plus real startup/readiness/liveness probes on `/health`, which neither `docker` nor `aci` have. Tested against a fake `CoreV1Api` (`tests/services/test_desktop_k8s.py`, 31 tests) - no live cluster needed, same pattern as the ACI tests' fake management client. NetworkPolicy/PodDisruptionBudget/dedicated ServiceAccount are deliberately out of scope (that hardening lives in the static `aks` template for anyone who wants it); a self-hoster wanting network isolation adds their own. |
| AKS Bot Desktop pool (`bot_desktop_mode=aks`, static template) | **partial** | The older path: a human `sed`s and `kubectl apply`s `infra/bot-desktop/k8s/desktop-template.yaml`, the API just records "pending" and never reconciles - `apps/worker` has no k8s code at all. Also referenced in the template behind Bicep's `deployBotDesktopAks`, off by default, never deployed. Superseded by `bot_desktop_mode=k8s` above for anyone who wants the API to actually drive the cluster; this is kept for the manual/CI deployment it was built for. |
| Temporal in Azure    | **missing** | Not provisioned. Point at Temporal Cloud or self-host                                                                                                          |
| Bot Desktop image    | **done**    | `infra/bot-desktop` builds Debian + XFCE + KasmVNC + sidecar                                                                                                   |
| Production deploy    | **missing** | Never run. Follow the checklist in `deploy.md` before trusting it                                                                                              |

## Documentation

| Document           | State                                                                          |
| ------------------ | ------------------------------------------------------------------------------ |
| `API.md`           | **done** — the binding contract                                                |
| `architecture.md`  | **done** — planes, chat-turn lifecycle, desktop lifecycle, routing, data model |
| `local-dev.md`     | **done** — setup, four runnables, mock modes, failure modes                    |
| `connectors.md`    | **done** — manifest, risk model, approvals, secret binding, worked example     |
| `bots.md`          | **done** — YAML schema, the five system bots, adding one                       |
| `security.md`      | **done** — including an explicit known-gaps list                               |
| `deploy.md`        | **done** — env matrix and production checklist                                 |
| Runbooks / on-call | **missing**                                                                    |

## The short list

If you are picking up work, these are the highest-value gaps:

1. **Build the AKS desktop reconciler.** `apps/worker` has zero aks/pod code;
   `services/desktop.py` can only ever set `state="starting"` and stop. This is
   not "exercise it end to end" — the worker-side half does not exist yet.
2. **Scope approvals/actions to a role, not just an owner.** RBAC is still
   fully missing — every authenticated user is equal once past ownership
   checks. See the design sketch in `security.md`.
3. **Session token refresh.** Logout/revocation now exists; there is still no
   way to renew a token short of signing in again.
4. **Run the Key Vault + Graph/generic_http live path against real
   credentials at least once.** Code is done; nobody has proven it against an
   actual vault, tenant, or connector yet.
