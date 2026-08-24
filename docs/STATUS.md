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
| Session tokens              | **partial** | HS256, 14 days. No refresh, no revocation                                                                                                                                                                                             |
| Ownership / visibility      | **done**    | System bots shared, custom bots and threads owned; invisible objects 404 rather than 403                                                                                                                                              |
| Roles / RBAC                | **missing** | Every authenticated user is equal. See `security.md`                                                                                                                                                                                  |
| Schema + seeding on boot    | **done**    | `ensure_schema` + `seed_system`; fatal in production, tolerated in dev                                                                                                                                                                |
| Streaming turns             | **done**    | SSE `token`/`handoff`/`tool`/`approval`/`done`/`error`                                                                                                                                                                                |
| Risk gate + approvals       | **done**    | Connector, MCP and desktop actions all gated; execution replays the stored payload server-side                                                                                                                                        |
| Approval scoping            | **missing** | Any authenticated user can decide any system bot's approval                                                                                                                                                                           |
| Key Vault secret resolution | **partial** | `app/services/secrets.py` resolves `kv://`, `env://` and bare refs with caching and a no-logging contract. Never run against a real vault, and `_invoke_vendor` still returns mock payloads, so no connector reaches a vendor API yet |
| Rate limiting               | **missing** | Only the per-bot daily budget, which is a soft cap                                                                                                                                                                                    |

## Models and RAG

| Area                  | State                    | Notes                                                                                         |
| --------------------- | ------------------------ | --------------------------------------------------------------------------------------------- |
| Tier routing          | **done**                 | `nano`/`mini`/`reason`/`embed` by task class, mirrored in `@nesqbot/model-router`             |
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
| Desktop backend `aks`    | **partial**                         | API records `starting`; the pod path is not exercised end to end                |
| Temporal worker          | **done**                            | Workflows, activities, health file, graceful drain                              |
| Routine schedules        | **done when Temporal is reachable** | Sync on create/patch, delete on delete                                          |
| Routine inline fallback  | **mocked**                          | No Temporal ⇒ steps run inline, no real `workflow_id`                           |
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
| CI                          | **partial** | GitHub Actions: ruff, mypy, pytest, Bicep build/lint/what-if, Node typecheck, desktop bundle, image builds                                                                                                                    |
| Python tests                | **partial** | `apps/worker/tests` only. The API has none                                                                                                                                                                                    |
| API/protocol parity check   | **done**    | `npm run check:api --workspace @nesqbot/protocol` diffs the TS interfaces against `schemas.py` for field presence and nullability. Advisory, not wired into CI                                                                |
| TypeScript tests            | **missing** | No test runner configured in any package                                                                                                                                                                                      |
| Prettier                    | **done**    | Configured in the root `package.json` (no semicolons, 120 cols, matching the existing style); `.prettierignore` covers lockfiles, generated output and `docs/API.md`. The tree has been formatted, so `npm run lint` is green |
| Dependency scanning         | **missing** |                                                                                                                                                                                                                               |

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

1. **Scope approvals to a user or an approver role.** Today anyone can approve
   anything a system bot raises. This is the one that blocks real use.
2. **Implement Key Vault `secret_ref` resolution.** Until then no connector can
   hold a real credential.
3. **Tests for the API.** The worker has some; the control plane — the part
   that enforces the risk gate — has none.
4. **Exercise the AKS desktop path end to end.** It is the only major
   subsystem that has never run outside mock or local Docker.
