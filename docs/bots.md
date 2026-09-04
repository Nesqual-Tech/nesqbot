# Bots

A bot is a role, a system prompt, a budget, and a set of tools. There are two
kinds:

- **System bots** — defined as YAML in `bots/`, seeded into the database on API
  startup, `is_system = true`. They belong to the organisation, not to a user.
- **Custom bots** — created at runtime through `POST /api/bots`, owned by their
  creator, deletable.

## The YAML schema

One file per bot in `bots/`, named `{slug}.yaml`. Loaded by
`apps/api/app/services/seed.py` from `BOTS_DIR` (default `../../bots`, `/bots`
inside the API container).

```yaml
name: Chief of Staff # required — display name
slug: chief_of_staff # required — lower_snake_case, unique, immutable
role: Orchestrator # optional — one-line job description, shown in the UI
desktop_profile: icewm # optional — xfce (default) | icewm
daily_budget_usd: 5 # optional — soft cap, defaults to 5
email: chief.of.staff@nesqualtech.com # optional — identity, not an inbox
voice: | # optional — how this one writes
  Short and decisive. Names who owns what, and by when.
signature: "— Chief of Staff, Nesqual Tech" # optional — how they sign off
desktop_habits: | # optional — which applications they reach for
  Browser and a terminal. Keeps one notes file open as the handoff ledger.
system_prompt: | # required — the bot's standing instructions
  You are the Chief of Staff on Nesq Bot.
  Route work to Sales, Lead Generator, Ops, or Support.
  Track handoffs in the shared context ledger. Never send externally.
  Only escalate judgment calls to the human.
```

| Field              | Required | Notes                                                                                           |
| ------------------ | -------- | ----------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `name`             | yes      | Display name                                                                                    |
| `slug`             | yes      | Primary identity. Changing it creates a _new_ bot and orphans the old one's history             |
| `system_prompt`    | yes      | Multi-line via `                                                                                | `. This is the bot's contract with itself |
| `role`             | no       | Defaults to `""`                                                                                |
| `desktop_profile`  | no       | `xfce` (full desktop) or `icewm` (lighter, for bots that rarely need a GUI). Defaults to `xfce` |
| `daily_budget_usd` | no       | Soft cap. Defaults to `5`                                                                       |
| `email`            | no       | The From line on a draft. **Identity, not a mailbox** — nothing arrives here without an inbound source, and sending still waits for a human |
| `voice`            | no       | How this bot writes. Two sentences, not a style guide                                            |
| `signature`        | no       | How it signs off                                                                                 |
| `desktop_habits`   | no       | Which applications it reaches for on its own machine                                             |

Seeding rules — the surprising ones are worth internalising:

- The loader starts from built-in defaults in `seed.py` and **overlays** any
  valid YAML with a matching `slug`. A YAML file wins over the default.
- A malformed file is skipped with a warning, not fatal. The API still boots.
  Check the API log if a bot you expected is missing.
- Seeding is create-only for a *new* slug, but a system bot (`is_system`) that
  already exists is **reconciled**, not skipped: its `name`/`role`/
  `system_prompt`/`desktop_profile` are overwritten from the current YAML
  every time seeding runs. The persona four — `email`, `voice`, `signature`,
  `desktop_habits` — are the exception: they are seeded once, for a bot that
  has none, and never written over again. The app promises exactly that ("the
  standing prompt is locked. Voice, email, signature and budget are yours to
  tune"), so re-applying the YAML on every boot would quietly undo somebody's
  edit and look like the save had failed. A custom bot is never touched — reconciliation only
  applies to `is_system` rows. Seeding runs on boot and on
  `POST /api/bots/system/reseed` (no restart needed for either a new bot or an
  edited prompt). `daily_budget_usd` is the one field seeding never overwrites
  once a bot exists, system or custom — an operator's tuned budget survives a
  YAML edit; change it with `PATCH /api/bots/{bot_id}/budget`.
- The API also seeds the first-party connector catalog and two starter KB
  articles on an empty database.

### Prompt guidance

The five system prompts are short on purpose. Each one states what the bot
does, what it prefers, and — for anything customer-facing — what it must never
do unilaterally. Three rules that have earned their place:

1. **Name the refusal.** "Never send externally", "Never auto-send", "0 sent
   until approved". The risk gate enforces it anyway, but a bot that expects to
   be stopped writes better drafts than one that is surprised.
2. **State the tool preference.** "Prefer CRM connectors; use Bot Desktop only
   when no API exists." Desktop automation is slow, brittle and expensive.
3. **Say how to report.** "Always report `N drafts, 0 sent`." Consistent
   reporting is what makes an overnight run reviewable in thirty seconds.

## The five system bots

| Bot            | Slug             | Role                         | Desktop | Budget | What it actually does                                                                                                                                                                                         |
| -------------- | ---------------- | ---------------------------- | ------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chief of Staff | `chief_of_staff` | Orchestrator                 | `icewm` | $5     | Intake and routing. Picks the specialist for a request, tracks the handoff in the shared context ledger, nudges stalls, compiles briefs. Never contacts anyone outside the company                            |
| Lead Generator | `lead_generator` | Outbound research and drafts | `xfce`  | $8     | Researches accounts overnight, scores intent, queues personalised email/LinkedIn drafts. Never auto-sends — reports "N drafts, 0 sent" until a human approves. Highest budget because research is token-heavy |
| Sales          | `sales`          | CRM and follow-ups           | `xfce`  | $5     | CRM hygiene, follow-ups in the seller's voice, flags stalls and commit risk, Monday scoreboards. Prefers CRM connectors over driving a browser                                                                |
| Ops            | `ops`            | Inbox, invoices, onboarding  | `xfce`  | $5     | Triages the shared inbox, extracts invoices into bookkeeping packets, runs new-hire checklists, flags calendar conflicts                                                                                      |
| Support        | `support`        | Tickets and KB               | `xfce`  | $5     | Classifies tickets, drafts KB-grounded replies with citations, escalates with context packs, sends close-the-loop reminders. Leans on the ticketing connector plus KB retrieval                               |

Chief of Staff is the front door. In a multi-bot thread it usually takes the
first turn, then hands off — the handoff appears to the client as a `handoff`
SSE event and to the next bot as an entry in `context_ledger`.

## Per-bot capability

Everything below is attached to a bot, not to a prompt:

| Capability  | Endpoint                                    | Notes                                      |
| ----------- | ------------------------------------------- | ------------------------------------------ |
| Connectors  | `POST /bots/{id}/connectors/{connector_id}` | With a `secret_ref`. See `connectors.md`   |
| MCP servers | `POST /bots/{id}/mcp/{mcp_id}`              | Tool allowlist applies                     |
| Bot Desktop | `POST /bots/{id}/desktop/start`             | One desktop per bot, at most               |
| Routines    | `POST /routines`                            | Cron-scheduled step lists, run by Temporal |
| Memories    | `POST /bots/{id}/memories`                  | Embedded, retrieved into the prompt        |
| Budget      | `PATCH /bots/{id}/budget`                   | Soft daily cap in USD                      |

Two bots with the same prompt and different bindings are different teammates.
Capability, not prompt wording, is the real configuration surface.

## Adding a custom bot

### As a system bot (checked into the repo)

1. Create `bots/finance.yaml`:

   ```yaml
   name: Finance
   slug: finance
   role: Bookkeeping and reconciliation
   desktop_profile: icewm
   daily_budget_usd: 4
   system_prompt: |
     You reconcile invoices against the ledger, flag anomalies over $500,
     and prepare month-end packets.
     Never pay anything. Never email a supplier. Draft, then stop.
   ```

2. `POST /api/bots/system/reseed`, or restart the API — seeding also runs on
   boot. Either way it is create-only for a new slug, and reconciles
   name/role/prompt/desktop_profile for an existing system bot without
   touching a custom bot's owner-tuned budget.
3. Bind its connectors and set its budget through the API or the desktop app.

### At runtime (owned by you)

```bash
curl -X POST localhost:8080/api/bots \
  -H 'X-Nesq-Dev: 1' -H 'Content-Type: application/json' \
  -d '{
    "name": "Finance",
    "role": "Bookkeeping and reconciliation",
    "system_prompt": "You reconcile invoices…",
    "connector_ids": ["invoice_portal"],
    "desktop_profile": "icewm",
    "daily_budget_usd": 4
  }'
```

The desktop app's **Builder** tab does the same thing with a form.

Custom bots differ from system bots in three ways: `is_system` is false,
`owner_user_id` is you, and `DELETE /api/bots/{bot_id}` works (it stops the
bot's desktop first). System bots reject a prompt or slug change with 403 and
cannot be deleted at all.

## Choosing a desktop profile

`icewm` for bots that mostly call APIs and only occasionally need a window —
it boots faster and uses noticeably less memory per pod. `xfce` for bots that
live in a browser or a desktop application. Chief of Staff and Finance are
`icewm`; the four specialists that drive real UIs are `xfce`.

A bot with no desktop started costs nothing to keep around. Start desktops
lazily and stop them when a routine finishes.
