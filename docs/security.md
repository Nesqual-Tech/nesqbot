# Security

Nesq Bot gives autonomous agents real credentials to real systems. The whole
design rests on one bet: **capability is cheap to grant, so the control has to
be at the point of action, not at the point of access.** A bot may read your
inbox all night; it may not send a single email without you.

This document describes what is enforced, where, and — at the end — what is
not yet enforced. The gaps section is the important one. Read it before you
point this at production data.

## Authentication

Two ways in, chosen by `NESQ_ENV`.

### Development bypass

In `NESQ_ENV=development`, `get_current_user` (`apps/api/app/auth.py`) resolves
a request to the seeded `dev@nesqualtech.com` user when **either**:

- there is no `Authorization` header, or
- the request carries `X-Nesq-Dev: 1`.

That is a full bypass, and it is what makes `curl` and the local apps work with
no login. `POST /api/auth/dev-login` mints a real token for the same user and
answers **403 `dev_login_disabled`** when `NESQ_ENV=production`.

The bypass is gated on exactly one environment variable. Treat `NESQ_ENV` as a
security control: an environment that ships to production with
`NESQ_ENV=development` is fully open to anyone who can reach the API.

### Microsoft Entra

`POST /api/auth/entra` takes the `id_token` from MSAL and verifies it properly:

- JWKS is fetched from `https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys`
  and cached (`entra_jwks_cache_seconds`), with a forced refresh on an unknown `kid`
  so key rollover does not cause an outage.
- Signature, expiry, `aud` (`AZURE_CLIENT_ID`) and `iss` (tenant allowlist) are
  all checked. An unconfigured tenant answers 400 `entra_not_configured`
  rather than silently trusting the token.
- The user is upserted by the immutable `oid` claim, never by email.

The API then issues **its own** session token: HS256 signed with `JWT_SECRET`,
`sub` = internal user id, 14-day expiry. Every subsequent request carries it as
`Authorization: Bearer <jwt>` and is re-checked against the `users` table, so a
deleted user's token stops working immediately.

`JWT_SECRET` defaults to `dev-change-me`. In production it must come from Key
Vault. Anyone holding it can mint a token for any user id.

## The risk model

Every action a bot can take is classified. The classification, not the prompt,
decides what happens.

| Class     | Example                              | Runs unattended |
| --------- | ------------------------------------ | --------------- |
| `observe` | read the inbox, search the CRM       | yes             |
| `draft`   | write a reply into the drafts folder | yes             |
| `mutate`  | update a CRM field, create a task    | yes             |
| `send`    | send an email, post a ticket reply   | **no**          |
| `spend`   | pay an invoice, buy credits          | **no**          |
| `delete`  | wipe a bot home, delete records      | **no**          |

`requiresApproval(risk)` in `@nesqbot/protocol` is the single implementation of
that table. The API, the connector SDK and both clients call it rather than
re-listing the classes, so there is exactly one place to change and no client
can disagree with the server.

The gate applies to all three execution paths:

- **Connector actions** — `POST /bots/{id}/connectors/{cid}/actions/{action}`
  answers `201 {approval_id, status:"pending_approval"}` instead of executing.
- **MCP tool calls** — held as an `mcp_tool` approval payload.
- **Bot Desktop actions** — a click that lands on a Send button is still a
  `send`. A bot cannot escape governance by driving a GUI instead of an API.

Approvals cannot be self-serviced by the agent: the decision endpoint requires
an authenticated human user, deciding a non-`pending` approval answers 409, and
execution happens server-side by replaying the stored payload — the client
never gets to say _what_ runs, only _whether_ it runs.

## Secrets

- Connector credentials are referenced, never stored. `bot_connectors.secret_ref`
  holds a reference in one of three forms — `kv://vault/secret-name`,
  `env://VAR_NAME` for local development, or a bare `secret-name` resolved
  against `AZURE_KEY_VAULT_URL`. The value lives in Key Vault.
- Secrets are resolved server-side at execution time and passed to the
  connector as `ConnectorContext.secrets`. They never reach a client, are never
  written to `audit_events`, and are not in any API response body.
- Bindings are per bot. Two bots using the same connector use different
  credentials, which is what makes "Support can read the ticket queue but not
  the sales inbox" expressible.
- Rotation is a new Key Vault version. `secret_ref` does not change and no bot
  needs rebinding.
- `JWT_SECRET`, `AZURE_OPENAI_API_KEY`, the Postgres password and the Entra
  client secret are Container App secret references in production, not
  environment literals in a template.

`.env` is gitignored. `.env.example` carries names and shapes only.

## Audit trail

`audit_events` is append-only: `(actor_user_id, bot_id, event_type, detail,
created_at)`. Nothing in the API updates or deletes a row. Approvals additionally
record `decided_by` and `decided_at`, so every gated action has a named human
attached to it.

`GET /api/audit?bot_id=&event_type=&limit=&before=` is the read path, newest
first.

Separately, every request gets an `X-Request-Id` (stamped by
`RequestContextMiddleware`, echoed in 500 bodies and exposed as a CORS response
header). Quote it in a bug report; it is the join key into the API logs.

## Tenancy and ownership

Ownership is by column, and visibility is derived:

| Object             | Rule                                                                     |
| ------------------ | ------------------------------------------------------------------------ |
| `bots`             | System bots are visible to everyone. Custom bots only to `owner_user_id` |
| `threads`          | Only `owner_user_id`                                                     |
| `messages`, `runs` | Inherit the thread's visibility                                          |
| `approvals`        | Inherit the bot's visibility                                             |
| `routines`         | Inherit the bot's visibility                                             |
| `mcp_servers`      | Only `owner_user_id`                                                     |
| `connectors`       | Catalog is shared; **bindings** are per bot                              |
| `memories`         | Scoped to bot and user                                                   |
| `kb_articles`      | Organisation-wide by design                                              |

Invisible objects answer **404, never 403**, so the API does not leak the
existence of another user's rows.

## Bot Desktop isolation

One container per bot, one home directory per bot
(`data/bot-homes/{bot_id}`, Azure Files in production). Bots cannot read each
other's sessions, cookies or downloads. Homes persist across restarts so a bot
keeps its logins; `POST /desktop/stop?wipe=1` destroys the home and is itself
`delete`-class.

The desktop stream (KasmVNC) is reachable by any user who can see the bot. For
system bots that is everyone — see the gaps below.

## Known gaps

Honest list, as of this commit. None of these are hypothetical; they are all
things you can verify in the code.

**No roles or RBAC.** Every authenticated user is equal. There is no admin, no
read-only viewer, and no approver role. Anyone who can log in can register a
connector, create a bot, and change a budget. A design sketch for closing this
follows the gaps list, below — it is a proposal, not something implemented.

**Secret resolution and vendor calls are implemented but unexercised against
real infrastructure.** `app/services/secrets.py` resolves `kv://vault/name`,
`env://VAR`, and bare names against `AZURE_KEY_VAULT_URL`, via
`DefaultAzureCredential`, with a five-minute cache and an explicit contract
that a resolved value never reaches a log line, response body, or audit event.
`_invoke_vendor` dispatches a resolved credential to a real driver —
`vendors/graph.py` for Microsoft Graph mail, `vendors/generic_http.py` for any
connector whose manifest sets `base_url` — once that base URL is configured.
What has not happened is a deployment: no connector has been bound to a real
Key Vault secret and called against a live tenant.

**`mutate` runs unattended, and prompt injection is real.** A bot that reads an
email containing instructions may act on them. The approval gate stops anything
reaching the outside world, but `mutate` — CRM writes, task creation, internal
record changes — is not gated. Assume a hostile inbox can cause internal data
churn, and keep `mutate` actions genuinely reversible.

**MCP tool risk is unknown by construction.** Tools are discovered live, so
there is no manifest to classify them from. The `tool_allowlist` is the only
control; keep it tight and treat an empty allowlist as the default.

**CORS falls back to `*` with credentials.** `allow_origins=cors_origin_list or ["*"]`
in `main.py` means an empty `CORS_ORIGINS` allows every origin. Always set it
explicitly in production.

**No rate limiting and no request quotas.** The only spend control is the
per-bot daily budget, which is a _soft_ cap checked before a turn — it does not
kill work in flight and it does not limit request volume.

**Session tokens have no refresh flow.** `POST /auth/logout` revokes the
presented token immediately (`jti` recorded in `revoked_tokens`, checked on
every request, pruned at boot once expired) — there is no logout-everywhere
across a user's other sessions, and a token cannot be renewed short of signing
in again, so a 14-day expiry is the only alternative to a fresh login.

**No encryption beyond the platform's.** Message content, memories and KB
articles are stored in plain columns, protected only by Postgres and disk
encryption. Do not put secrets in a chat message.

**Desktop stream authorisation is coarse.** Anyone who can see a bot can watch
its screen and, through `/desktop/action`, drive it.

**No SAST, and dependency scanning is advisory only.** `ci.yml` runs
`pip-audit`/`npm audit` and `dependabot.yml` opens weekly update PRs, but both
are `continue-on-error` until a first pass of existing findings is triaged —
neither currently blocks a merge. There is no static application security
scan. (The API itself has 1800+ passing tests — see `STATUS.md` — this gap is
about scanning, not about test coverage.)

## A design sketch for roles

Not implemented. Approval and object *ownership* is enforced today (see
Tenancy and ownership, above) — what is missing is *authorization by role*
layered on top of it, for the handful of actions ownership alone cannot gate:
who may register a connector or MCP server, create a system bot, change a
daily budget, or approve on behalf of a shared system bot rather than only
their own.

A minimal shape that fits the existing schema without a migration framework:

- **A `role` column on `users`** (`TEXT NOT NULL DEFAULT 'member'`), values
  `admin`, `member`, `viewer`. Same `IF NOT EXISTS` / `ADD COLUMN IF NOT
  EXISTS` idempotent pattern `sql/init.sql` already uses everywhere else.
- **A small `require_role(*roles)` FastAPI dependency** next to
  `get_current_user`, composed onto the handful of routes that need it —
  connector/MCP registration, system-bot creation, budget changes — the same
  way `get_visible_bot`/`get_visible_approval` compose onto the ownership
  checks now. Most routes would not change at all: ownership already gates
  them correctly.
- **An `approver` grant, separate from role**, for the system-bot approval
  problem specifically: today `get_visible_approval` falls back to *bot*
  visibility only when an approval has no knowable owner (a routine step
  against a shared system bot). A `bot_approvers(bot_id, user_id)` table would
  let that fallback narrow to a named set of people per system bot instead of
  "everyone who can see the bot" — closing the approval-scoping gap for the
  shared-bot case the same way owner-resolution already closed it for the
  per-user case.
- **404, not 403, stays the rule.** A role check that fails should look
  identical to the object not existing, consistent with every other
  authorization check in this codebase.

This is deliberately small: one column, one dependency, one join table. A
fuller permission matrix (per-connector grants, delegated admin, audit of role
changes themselves) is real future work, but the object above is enough to
close "anyone who can log in can register a connector" without redesigning
anything that already works.

## If you are hardening this for production

In rough priority order:

1. Implement roles (see the design sketch above) and gate connector/MCP
   registration, system-bot creation, and budget changes behind `admin`.
2. Exercise secret resolution end to end: bind one connector to a real Key Vault secret against a deployed managed identity, and confirm the value never lands in a log, response, or audit row.
3. Add a `bot_approvers` grant so a shared system bot's approvals are decidable by a named set of people, not everyone who can see the bot.
4. Set `CORS_ORIGINS` explicitly and fail startup if it is empty in production.
5. Move `JWT_SECRET` to Key Vault, shorten the session to hours, add a refresh endpoint.
6. Add rate limiting at the ingress.
7. Gate `mutate` on connectors that touch customer-visible records.
8. Triage the existing `pip-audit`/`npm audit` findings and drop `continue-on-error` once clean.
