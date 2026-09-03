# Local development

Goal: four processes running against Docker infrastructure, with every Azure
dependency mocked so you can work offline and without keys.

## Prerequisites

| Tool   | Version      | Notes                                                         |
| ------ | ------------ | ------------------------------------------------------------- |
| Node   | >= 20        | `engine-strict=true` in `.npmrc`, so an older Node fails fast |
| npm    | >= 10        | workspaces; `packageManager` pins `npm@10.9.2`                |
| Python | 3.11 or 3.12 | for `apps/api` and `apps/worker`                              |
| Docker | any recent   | Postgres, Redis, Temporal                                     |

## One-time setup

```bash
cp .env.example .env

# JS workspaces — one install at the root covers apps/* and packages/*
npm install

# infrastructure
docker compose up -d postgres redis temporal

# Python API
cd apps/api
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
cd ../..

# Python worker (separate venv, separate requirements)
cd apps/worker
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cd ../..
```

`npm install` must be run at the **root**. Installing inside `apps/desktop` or
a package directly breaks the workspace symlinks that make `@nesqbot/ui` and
`@nesqbot/protocol` resolvable.

The API creates its schema and seeds the five system bots on first boot
(`ensure_schema` + `seed_system` in `apps/api/app/main.py`), so there is no
migration step for a fresh database.

## The four runnables

Each in its own terminal. All root scripts:

```bash
npm run api        # FastAPI on :8080   (needs apps/api/.venv activated)
npm run worker     # Temporal worker    (needs apps/worker/.venv activated)
npm run desktop    # Vite dev server on :1420
npm run mobile     # Expo dev server on :8081
```

| Process        | Port | Health check                       |
| -------------- | ---- | ---------------------------------- |
| API            | 8080 | <http://localhost:8080/api/health> |
| Desktop (Vite) | 1420 | browser opens automatically        |
| Mobile (Expo)  | 8081 | QR code in the terminal            |
| Postgres       | 5432 | `docker compose ps`                |
| Redis          | 6379 | `docker compose ps`                |
| Temporal       | 7233 | UI on <http://localhost:8088>      |

You do not need all four. The desktop app plus the API covers most work; the
worker is only needed for scheduled routines, and mobile only for approvals
and the live desktop view.

Everything in Docker instead:

```bash
docker compose up -d          # postgres, redis, temporal, temporal-ui, api, worker
```

The compose `api` service already sets `BOT_DESKTOP_MODE=mock`, `BOTS_DIR=/bots`
and container-internal `DATABASE_URL` / `REDIS_URL` / `TEMPORAL_HOST`, so it
ignores the host values in your `.env` for those keys.

## Workspace layout and the source-only packages

```
apps/desktop     @nesqbot/desktop   Tauri 2 + React + Vite
apps/mobile      @nesqbot/mobile    Expo / React Native
apps/api         (Python)           FastAPI control plane
apps/worker      (Python)           Temporal worker
packages/ui      @nesqbot/ui        design tokens + semantic roles
packages/protocol      @nesqbot/protocol       API types, risk helpers, SSE union
packages/model-router  @nesqbot/model-router   tier table + cost estimation
packages/connector-sdk @nesqbot/connector-sdk  manifest authoring + validation
```

The four packages ship **TypeScript source**, not compiled JavaScript. Their
`main`, `types` and `exports` fields all point at `./src/index.ts`. Vite and
Metro compile it directly, which is what makes editing a token in
`packages/ui` hot-reload in the desktop app without a build step.

Consequences to know about:

- Do not "fix" a package's `exports` to point at `dist/`. There is a comment in
  every `package.json` saying so.
- `npm run build` / `npm run typecheck` emit into `packages/*/dist` — that is
  declaration output for CI and editors, not something anything imports.
  `npm run clean` removes it.
- Because they are composite TypeScript projects, `tsc -b` _is_ the typecheck.
  Both scripts run it; the second invocation is a no-op thanks to
  `.tsbuildinfo`.
- Node cannot `require()` these packages. Nothing does — they are consumed only
  by bundlers and by `tsc`.

The two apps resolve the packages differently, and both are correct:

- **Desktop** declares them as workspace dependencies
  (`"@nesqbot/ui": "*"`) and _also_ keeps a Vite `resolve.alias` and tsconfig
  `paths` entry. The alias is belt-and-braces — it keeps a partially
  installed tree working.
- **Mobile** does not declare them at all. Metro resolves them through
  `extraNodeModules` in `apps/mobile/metro.config.js` plus tsconfig `paths`,
  pointing straight at `packages/*/src`. Metro also needs the repo root in
  `watchFolders`, which that config sets. If a mobile import of
  `@nesqbot/ui` stops resolving, that file is the first place to look.

`.npmrc` pins `install-links=false`. Per npm's own config definition that
setting only affects `file:` specifiers and has no effect on workspaces, so
the `"*"` deps would symlink either way — but a `file:../../packages/ui`
specifier under `install-links=true` would be silently _copied_, freezing the
package at install time. Do not set it back to true, and prefer `"*"` over a
`file:` path when adding a workspace dependency.

## Mock modes

Nothing in the local loop requires an Azure subscription.

| Missing dependency                                                        | What happens                                                                                                                              | How to tell                                  |
| ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| **Azure OpenAI** (`AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` empty) | The model router returns a deterministic canned reply and still writes a cost ledger row using estimated tokens                           | Replies are prefixed `[mock:mini]`           |
| **Azure embeddings**                                                      | `embed()` returns `None`; memory and KB search fall back to keyword `ILIKE` matching                                                      | KB hits stop being semantic                  |
| **Redis**                                                                 | `services/events.publish` falls back to an in-process pubsub, so SSE still works within one API process                                   | Events stop arriving in a second API replica |
| **Temporal**                                                              | `POST /routines/{id}/run` executes the steps inline instead of starting a workflow; schedule sync is skipped                              | Response carries no real `workflow_id`       |
| **Bot Desktop** (`BOT_DESKTOP_MODE=mock`)                                 | Lifecycle transitions are recorded without a container; `/screenshot` returns a generated placeholder PNG                                 | `container_id` looks like `mock-sales`       |
| **Entra** (`AZURE_TENANT_ID` etc. empty)                                  | Dev auth: any request in `NESQ_ENV=development` without an `Authorization` header, or with `X-Nesq-Dev: 1`, becomes `dev@nesqualtech.com` | `GET /api/me` works with no token            |

To exercise a real Bot Desktop locally:

```bash
docker build -t nesqbot/bot-desktop:local infra/bot-desktop
# .env
BOT_DESKTOP_MODE=docker
BOT_DESKTOP_IMAGE=nesqbot/bot-desktop:local
BOT_DESKTOP_NETWORK=nesqbot_default
```

## Common failures

**`Cannot find module '@nesqbot/ui'`**
Root `npm install` was never run, or it was run inside an app directory. A
leftover `apps/*/node_modules` from a standalone install shadows the hoisted
workspace tree: delete it and the root `node_modules`, then `npm install` at
the root. On Windows without Developer Mode, npm may fail to create the
workspace symlinks — run the terminal as administrator once.

**`Unsupported engine` on install**
`.npmrc` sets `engine-strict=true` and the repo requires Node >= 20. Upgrade
Node; do not remove the flag.

**API starts but every request 500s on the database**
Postgres is up but the `vector` extension is missing. The compose file mounts
`apps/api/sql/init.sql` as an init script, which only runs on an _empty_ data
directory. If you created the volume before that was wired:
`docker compose down -v && docker compose up -d postgres` (this deletes local
data).

**CORS errors in the desktop app**
`CORS_ORIGINS` in `.env` must contain the dev server origin —
`http://localhost:1420` for Vite, `http://localhost:8081` for Expo web.

**A packaged desktop build cannot reach your own hosted API**
Not CORS — the Tauri CSP. `apps/desktop/src-tauri/tauri.conf.json` allowlists
the hosts the packaged app may talk to, and it ships with `localhost:8080` (so
a local API works out of the box) plus a `your-api.example.com` placeholder.
Pointing `VITE_API_URL` — or the setup wizard's runtime override — at your own
API is not enough on its own: add that origin to `connect-src`, `img-src` and
`frame-src`, and its `wss://` form to `connect-src`, or the requests are
blocked before they leave the app. The dev server is not affected, which is why
this only shows up after packaging.

**Expo cannot resolve a workspace package**
Metro does not follow symlinks out of the app directory by default. The mobile
app needs a `metro.config.js` that adds the repo root to `watchFolders` and
enables package exports. Restart with `npx expo start -c` after changing it —
Metro caches resolution aggressively.

**Mobile app cannot reach the API**
`localhost` on a phone is the phone. Point the mobile API base at your
machine's LAN IP and make sure the API is bound to `0.0.0.0` (the `npm run api`
script does this).

**Port already in use**
1420 (Vite, `strictPort`), 8080 (API), 8081 (Expo), 5432, 6379, 7233, 8088.
`docker compose down` clears the infrastructure ones.

**Approvals never arrive in the client**
Check that the run actually parked: `GET /api/runs?status=awaiting_approval`.
If the run completed instead, the action's risk class was below the approval
threshold — see `connectors.md`.

**Temporal container restarts in a loop**
It shares the Postgres instance and needs it healthy first. `docker compose up
-d postgres` and wait for the health check before bringing up `temporal`.

## Checks before you push

```bash
npm run typecheck     # tsc -b across every workspace
npm run lint          # prettier --check
npm run build         # declaration output for the packages

# If you touched apps/api/app/schemas.py or packages/protocol:
npm run check:api --workspace @nesqbot/protocol
```

`check:api` compares the TypeScript interfaces in `@nesqbot/protocol` against
the pydantic models in `apps/api/app/schemas.py` — field presence and, more
importantly, **nullability**. It exists because presence-only review missed two
real drifts (`UsageSummary.entries`, `Run.thread_id`), both of which were
latent: nothing rendered the field yet, so the first component to trust the
type would have inherited the bug. It is a lint for that one failure mode, not
a schema compiler — a clean run means the fields line up, nothing stronger.
Deliberate deviations are listed in the script's `EXEMPT` table with reasons.

There is no test suite yet and no Python linting wired into these scripts —
see `STATUS.md` for the honest list of what is missing.

## Clicking the app (`make harness`)

```bash
make harness        # api + postgres + the desktop dev server, all throwaway
make harness-down   # delete all of it
```

Prints a URL. The setup wizard asks for the backend
(`http://localhost:18080/api`), auth is the development bypass so there is no
sign-in, and with no model credentials every bot answers with the router's
deterministic mock — enough to exercise send, streaming, tool calls, work
items, approvals, and whether the transcript survives a tab switch.

It exists because four user-visible bugs shipped in one day past a green
`pytest` and a clean `tsc`: a broken chat layout, a wizard rendering "signal is
aborted without reason", turns that failed in silence, and a reasoning
deployment sitting at 100% of its token quota. Every one was found by opening
the app and clicking Send. Neither test suite was ever going to catch "the
reply never arrives" — both were passing while it did not arrive.

It exports `HEAD` to a local drive rather than serving the repo directly,
because npm, esbuild and `npx` all fail over a UNC share (`\NAS\...`), and
Docker cannot bind-mount from one either. That also means it tests **committed**
code: commit, then click.
