# Nesq Bot

**Source-available** always-on AI teammates. Each bot gets its own isolated
Linux desktop, drives a real browser, and **stops for a human before anything
consequential** — sending, spending, deleting. Bots hand work to each other and
every handover is written to a transfer ledger.

Source-available, not open source. You may read it, run it, audit it, modify it
and self-host it. You may not sell it as a competing product or service. Each
release becomes Apache 2.0 two years after it ships. See [LICENSE](LICENSE)
(FSL-1.1-ALv2) and [the licence section](#licence) below.

```bash
git clone <this repo> && cd nesqbot
docker compose up -d
curl -H 'X-Nesq-Dev: 1' http://localhost:8080/api/bots
```

That is the whole first run. No Azure account, no API key, no model provider —
the model router mocks itself when it has no endpoint, so you get a seeded
database and five bots you can talk to immediately. See
[Quick start](#quick-start).

---

## Why this is different

Most agent products ask you to trust a paragraph on a marketing page. The point
of publishing this one is that **you can read the safety code yourself**, and it
is small enough to actually read.

### One classifier, not a policy engine

[`apps/api/app/services/risk.py`](apps/api/app/services/risk.py) is the single
risk classifier the whole application shares — desktop primitives, browser DOM
actions and MCP tool names all resolve through it. Six levels, least to most
dangerous:

```
observe  →  draft  →  mutate  →  send  →  spend  →  delete
```

`send`, `spend` and `delete` stop and wait for a person. Unrecognised actions
default to `mutate`, not `observe`, so a tool nobody has heard of is never
assumed harmless. A connector's manifest may **raise** a classification but
never lower it.

The file is ~230 lines. Read it and you know exactly what the product will and
will not do on its own.

### One chokepoint, not a convention

[`apps/api/app/services/simulation.py`](apps/api/app/services/simulation.py) —
`perform()` is the single function every outbound effect in the service layer
passes through: connector actions, MCP tool calls, desktop actions, and the
approval steps that hold them. It classifies, runs preflight, and then either
records the intent or performs it.

The private `_execute()` **refuses to run** while a simulation context is
active. That is what makes a dry run structurally honest rather than honest by
convention: a new step type cannot quietly acquire a side effect, because it
either goes through `perform` and is simulated, or it trips the guard.

Reading that function does not open the gate. The gate is the shape of the code.

### An audit trail that deleting a record cannot erase

`audit_events` and `work_item_transfers` carry **no foreign keys** — see
[`apps/api/sql/init.sql`](apps/api/sql/init.sql). Deleting a thread, a bot or a
work item cascades through the operational tables and leaves the history
standing. An audit row that a `DELETE` can take with it is not an audit row.

### Real isolation, per bot

Each bot gets its own desktop — a container group of its own, never shared,
never reused between bots, and never given a public IP. On Azure that is
hypervisor-isolated ACI; locally it is a container; in tests it is a mock. The
naming function in
[`apps/api/app/services/desktop.py`](apps/api/app/services/desktop.py) suffixes
the bot id precisely so two similar slugs can never collide onto one group.

### Delegation grants no authority

A bot can hand work to another bot. The receiving bot's actions are classified
by the same `risk.py` and pass through the same `perform()` chokepoint they
would if a person had asked. **Being delegated to is not a reason anything
skips an approval.** Every handover writes a `work_item_transfers` row, and no
code path can move `work_items.owner_bot_id` without writing one.

---

## Quick start

Requirements: Docker (with Compose v2). Nothing else.

```bash
docker compose up -d
```

That brings up Postgres with pgvector, Redis, Temporal, the API and the worker.
The API creates its schema and seeds the five system bots on first boot. Give it
about a minute the first time; watch it with `docker compose logs -f api`.

```bash
# health
curl http://localhost:8080/api/health

# the seeded bots
curl -H 'X-Nesq-Dev: 1' http://localhost:8080/api/bots

# talk to one
BOT=$(curl -s -H 'X-Nesq-Dev: 1' http://localhost:8080/api/bots \
      | grep -o '"id":"[^"]*","slug":"chief_of_staff"' | cut -d'"' -f4)
TID=$(curl -s -H 'X-Nesq-Dev: 1' -H 'Content-Type: application/json' \
      -X POST http://localhost:8080/api/threads \
      -d "{\"title\":\"first run\",\"bot_ids\":[\"$BOT\"]}" | cut -d'"' -f4)
curl -H 'X-Nesq-Dev: 1' -H 'Content-Type: application/json' \
     -X POST "http://localhost:8080/api/threads/$TID/messages" \
     -d '{"content":"Hello — who are you and what can you do?"}'
```

You will get a reply prefixed `[mock:mini]`. That is the model router telling
you, honestly, that it has no endpoint configured and is returning canned text.
Everything else on the path is real: the thread, the run, the risk
classification, the audit rows and the cost ledger.

**`X-Nesq-Dev: 1`** is the development auth bypass. It only works while
`NESQ_ENV=development`, which is the default in the base compose file and is
refused outright in production — see `apps/api/app/config.py` and
[`docs/security.md`](docs/security.md).

### Configuration

Nothing needs configuring for the first run — `docker compose up` works against
a repo with no `.env` at all. To change anything:

```bash
cp .env.example .env
```

Every value in `.env.example` has a working default and is annotated with
`[required-prod]`, `[optional]` or `[compose]`. Setting `AZURE_OPENAI_ENDPOINT`
(plus a key, or a managed identity in Azure) is what switches the model router
off mock and onto a real provider.

If port 8080, 5432, 6379 or 7233 is busy on your machine, override
`API_HOST_PORT`, `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` or
`TEMPORAL_HOST_PORT` in `.env`.

### Opting in to more

```bash
# real per-bot desktops on the host Docker daemon
docker compose --profile desktop build bot-desktop
docker compose -f docker-compose.yml -f docker-compose.desktop-docker.yml up -d

# Temporal's own UI
open http://localhost:8088
```

### Running the apps

The desktop and mobile clients are not in the compose stack — they are dev
servers you run yourself, and they need Node 20+.

```bash
npm install          # root only; this is one npm workspace
npm run desktop      # Vite on :1420
npm run mobile       # Expo on :8081
```

`npm install` needs a local disk. It does not work reliably on a network share
or a mapped drive, because npm's symlinked workspace layout and the file
watchers behind Vite and Metro both require one.

Full setup, all four runnables, and the failure modes you will actually hit:
**[`docs/local-dev.md`](docs/local-dev.md)**.

---

## Architecture at a glance

| Layer       | Tech                                                                          |
| ----------- | ----------------------------------------------------------------------------- |
| Desktop     | Tauri 2 + React + Vite                                                        |
| Mobile      | Expo / React Native                                                           |
| API         | FastAPI (Azure Container Apps in production)                                  |
| Workers     | Temporal                                                                      |
| Bot Desktop | Debian + XFCE/IceWM + KasmVNC + agent sidecar; Docker locally, ACI in Azure   |
| Models      | Azure AI Foundry, tiered `nano` / `mini` / `reason` / `embed`; mocks unset    |
| Data        | PostgreSQL + pgvector, Redis, Blob, Files, Key Vault                          |

```
apps/desktop            @nesqbot/desktop         Tauri messenger + Bot Desktop pane
apps/mobile             @nesqbot/mobile          Expo chat / approvals / live desktop
apps/api                (Python)                 FastAPI control plane
apps/worker             (Python)                 Temporal workers
packages/ui             @nesqbot/ui              design tokens + semantic roles
packages/protocol       @nesqbot/protocol        API types, risk helpers, SSE union
packages/model-router   @nesqbot/model-router    tier table + cost estimation
packages/connector-sdk  @nesqbot/connector-sdk   connector manifest authoring
bots/                   specialist bot definitions (YAML)
infra/bot-desktop       OS image + sidecar
infra/azure             Bicep — exactly what runs where
docs/                   see below
```

The four packages export **TypeScript source**, not compiled output — Vite and
Metro compile them directly, so editing a token hot-reloads with no build step.
Do not repoint their `exports` at `dist/`; there is a comment in every
`package.json` explaining why.

## Bots

| Bot                | Does                                                             |
| ------------------ | ---------------------------------------------------------------- |
| **Chief of Staff** | Intake, routing, handoffs, briefs. Never contacts anyone outside |
| **Lead Generator** | Overnight research and draft queues. Never auto-sends            |
| **Sales**          | CRM hygiene, follow-ups, stall detection, Monday scoreboards     |
| **Ops**            | Shared inbox, invoices, onboarding checklists                    |
| **Support**        | Ticket triage, KB-grounded replies with citations                |

Defined as YAML in `bots/`, seeded on API startup. Add your own with a file or
through `POST /api/bots` — see [`docs/bots.md`](docs/bots.md).

## Documentation

| Document                                       | Read it when                                                                  |
| ---------------------------------------------- | ----------------------------------------------------------------------------- |
| [`docs/architecture.md`](docs/architecture.md) | You want the planes, the chat-turn lifecycle, and the data model              |
| [`docs/API.md`](docs/API.md)                   | You are calling or implementing an endpoint. **This is the binding contract** |
| [`docs/local-dev.md`](docs/local-dev.md)       | You are setting up, or something will not start                               |
| [`docs/bots.md`](docs/bots.md)                 | You are adding or tuning a bot                                                |
| [`docs/connectors.md`](docs/connectors.md)     | You are adding a tool, or wondering what needs approval                       |
| [`docs/security.md`](docs/security.md)         | Before you point this at real data. Has an honest known-gaps list             |
| [`docs/deploy.md`](docs/deploy.md)             | You are deploying to Azure                                                    |
| [`docs/entra-setup.md`](docs/entra-setup.md)   | You are turning on real sign-in                                               |
| [`docs/STATUS.md`](docs/STATUS.md)             | You want to know what is done, mocked, or missing                             |

[`docs/STATUS.md`](docs/STATUS.md) and the known-gaps list in
[`docs/security.md`](docs/security.md) are deliberately unflattering. A product
whose argument is auditability does not get to publish only the parts that
audit well.

## Tests

The API suite needs Docker and a Postgres with pgvector. It will start a
throwaway container for you:

```bash
cd apps/api
python -m venv .venv && . .venv/bin/activate    # Scripts/activate on Windows
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

Set `TEST_DATABASE_URL` to point at a Postgres you already have and it will use
that instead.

## Contributing

Issues and pull requests are welcome. Community support is best-effort — see
[CONTRIBUTING.md](CONTRIBUTING.md), which also covers the CLA that every
contribution requires, and [SECURITY.md](SECURITY.md) for vulnerability
reporting. **Please do not open a public issue for a security problem.**

## Licence

Licensed under the **Functional Source License, Version 1.1, with an Apache 2.0
future licence** (`FSL-1.1-ALv2`). The full text is in [LICENSE](LICENSE).

In plain terms, and the licence text governs where this summary is loose:

- **You may** use it internally, modify it, self-host it, run it for your own
  company, use it for non-commercial education and research, and provide
  professional services around it to someone else who is licensed under these
  same terms.
- **You may not** make it available to others as a commercial product or
  service that substitutes for it or offers substantially the same
  functionality. That is the one prohibited use, and it is the only one.
- **Two years after any given release ships, that release becomes Apache 2.0**,
  automatically and irrevocably. The restriction has an expiry date built into
  the licence.

This is **source-available**, not open source. Every accepted definition of open
source permits resale, and a licence that forbids it does not meet them. Calling
it otherwise would be inaccurate, so we do not.

Nesq Bot and the Nesqual Tech marks are not licensed for use beyond identifying
the origin of the software.
