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
| Multi-provider models | **partial**              | `MODEL_PROVIDER` (global + per-tier) picks `azure` (default, unchanged) or `openai` — real OpenAI or any self-hosted OpenAI-compatible server ("local models": Ollama, vLLM, LM Studio, OpenRouter) via `OPENAI_BASE_URL`. `anthropic`/`google` are accepted config values with no client yet — a tier routed to either falls back to mock, on purpose, rather than guessing at an unbuilt wire format. No per-bot override, no setup wizard UI. See `services/model_router.py` |
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
| Desktop (Tauri + React) | **partial** | Bots, chat, approvals, integrations, usage, builder. Tauri shell builds; CI builds the web bundle only |
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
| AKS Bot Desktop pool | **partial** | In the template behind `deployBotDesktopAks`, off by default, never deployed                                                                                   |
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
