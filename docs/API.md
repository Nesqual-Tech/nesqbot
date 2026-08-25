# Nesq Bot API contract (v0.3 — target surface)

All routes under `/api`. Auth: `Authorization: Bearer <jwt>` or `X-Nesq-Dev: 1` in development.
All list endpoints are scoped to the calling user where an owner column exists.

## Health / auth

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | shallow `{ok, service, version, build}` — `version` is the hand-maintained contract number, `build` the image tag stamped at build time (`NESQ_BUILD`, `"unknown"` if unstamped). They move independently: never read one as the other. |
| GET | `/health/deep` | checks db + redis + temporal; `{ok, checks:{db,redis,temporal}}`; 503 if db down |
| POST | `/auth/dev-login` | dev only (403 when `NESQ_ENV=production`) |
| POST | `/auth/entra` | body `{id_token}` → validate Entra JWT via JWKS, upsert user by `oid`, return `TokenOut` |
| POST | `/auth/logout` | revoke the bearer token presented on this call; `{ok, detail: revoked\|nothing_to_revoke}` |
| GET | `/me` | current user |
| POST | `/me/devices` | register a push token: `{token, platform: ios\|android\|web}` → upsert on `(user_id, token)`, returns `{ok, device_id}`. Used by the mobile app for approval notifications |
| DELETE | `/me/devices/{token}` | unregister |

## Bots

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/bots` | list |
| POST | `/bots` | create custom |
| GET | `/bots/{bot_id}` | single, 404 if missing |
| PATCH | `/bots/{bot_id}` | `UpdateBotIn`: name/role/system_prompt/daily_budget_usd/desktop_profile; 403 changing prompt/slug on system bots |
| DELETE | `/bots/{bot_id}` | custom bots only (403 on `is_system`); stops desktop first |

### Bots working together

Routing and delegation are different things and the contract keeps them apart.

**Routing** picks which bot answers one human message. `Orchestrator._select_bot` matches keywords
in the *person's* text, falling back to a model call among the thread's slugs. Nothing is
transferred: no brief, no ownership, no state. A bot is not involved in the decision and cannot
influence it.

**Delegation** is a bot handing work to another bot — the lead bot passing a lead that answered to
the sales bot to close. It is driven by the `delegate_to_bot` control tool inside the agent loop,
not over HTTP, and it carries a brief. It does NOT move a work item: this sentence
once claimed it did, and `_delegate` has never contained a line of work-item code.
Ownership moves through `transfer_work_item`, which is a separate tool for the reasons
in invariant 5.

Both emit the **same `handoff` event**, because from a client's point of view both mean "a
different bot is answering now" and that is the thing already rendered. The delegated path adds
five optional keys (see the event table below); a client reading only `bot_id`/`bot_name` cannot
tell the two apart and does not need to. Read `delegated` when you do need to.

Four invariants hold whatever the mechanism looks like:

1. **The actor is the originating human, inherited down the whole chain.** A delegated run does
   *not* act as the delegating bot. This is what keeps approval owner-scoping working — approvals
   resolve their audience through `requested_by` → thread owner → custom bot owner, and a run whose
   actor is a bot has no audience, so nobody could decide it. It is also what lets the audit answer
   "on whose behalf", which is the question the governance surface exists to answer. The chain
   itself is recorded alongside the actor, so the trail reads `person → lead_generator → sales`
   rather than flattening to the person.
2. **Delegation is bounded and says why it stopped.** It is the one path that reaches a `send`,
   `spend` or `delete` action with no human turn in between, so an unbounded chain is an unbounded
   bill. Depth and total delegations per chain are capped, and hitting a cap is reported, never
   silent. Note that revisiting a bot is legitimate — sales asking lead-gen to enrich a record and
   getting it back is the intended shape — so "never revisit" is the wrong rule.
3. **Delegation grants no authority.** The receiving bot's actions are classified by
   `services/risk.py::classify_action_risk` and pass through `simulation.perform` exactly as they
   would if a person had asked. Being delegated to is not a reason anything skips an approval.
4. **The receiving bot does not start cold.** It gets the brief and the context needed to act. A
   bot that has to re-derive the request from scratch will re-do work the first bot already paid
   for, in tokens and in side effects.
5. **Transferring a work item and delegating are different acts, and stay separate tools.**
   `transfer_work_item` moves `work_items.owner_bot_id` and writes the ledger row; it starts
   nothing. `delegate_to_bot` starts a run and is capped at 3 hops, 6 per chain, 30 minutes.
   Fusing them breaks in both directions: a bot out of hops could no longer *hand a lead over
   at all* — a delegation cap refusing a database write — and a transfer would become a way to
   start a run without passing the one place that decides whether a run may begin. They
   compose, in that order: transfer, then delegate, so the handover survives a refused
   delegation.

## Threads / messages

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/threads` | own threads |
| POST | `/threads` | create |
| DELETE | `/threads/{thread_id}` | cascade |
| GET | `/threads/{thread_id}/messages` | ordered |
| POST | `/threads/{thread_id}/messages` | non-streaming turn (existing response shape, unchanged) |
| POST | `/threads/{thread_id}/messages/stream` | SSE `text/event-stream`. Events: `token` `{delta}`, `handoff` `{bot_id,bot_name,from_bot_id?,from_bot_name?,run_id?,chain?,delegated?}` (the last five appear only when one bot handed work to another through the `delegate_to_bot` control tool; `chain` is the delegation path, e.g. `person → lead_generator → sales`), `tool` `{connector,action,ok}`, `approval` `{approval_id,title,phase?,run_id?}` (`phase` is either `approved` or `rejected`, and `run_id` names the run being continued; both appear only on a decision that resumes a parked run), `desktop` `{bot_id,phase,…}`, `done` `{message_id,bot_id,tier,cost_usd}`, `error` `{detail}` |
| GET | `/threads/{thread_id}/events` | SSE subscription to thread events pushed by worker/routines. Carries `turn_started` `{thread_id,bot_id,bot_name}`, `handoff`, `tool`, `approval`, `desktop`, `done`, `error`. Deliberately does NOT carry `token` — the streaming requester gets deltas on its own response; passive viewers get a typing indicator from `turn_started` and the finished text from `done` |

### The `desktop` event

Emitted while a bot is driving its Bot Desktop during a turn. `phase` is one of:

| `phase` | Meaning |
| --- | --- |
| `starting` | The bot is bringing its desktop up. On ACI a cold start takes 30–90 seconds; the event carries a `detail` string saying so, and exists so the UI shows progress instead of a hung turn |
| `ready` | The desktop is running. Carries `state` |
| `unavailable` | The desktop would not start. Carries `detail` — the real reason. No desktop work was done |
| `blocked` | Starting the desktop is gated in this deployment and needs an approval. Carries `detail` |
| `finished` | The loop ended. Carries `steps` (how many desktop actions actually ran) and `approval_id` (set when a step was held for a human instead of running) |

Individual desktop actions are also reported through the ordinary `tool` event as
`{"connector": "desktop", "action": …, "ok": …}`.

### The `takeover` event

Emitted when an agent hits something only a human can do — a login, an MFA prompt, a CAPTCHA.
The run parks in `awaiting_human` and the state needed to continue is persisted on the run, so it
survives a restart.

```json
{
  "phase": "requested",
  "run_id": "…", "thread_id": "…", "bot_id": "…", "bot_name": "…",
  "reason": "LinkedIn is asking for a password",
  "what_you_need": "Sign in, then press continue",
  "resume_url": "/api/runs/…/resume"
}
```

The client shows the live desktop, lets the person do the sensitive step themselves, and calls
`POST /runs/{run_id}/resume`. The agent takes a fresh screenshot to see what changed and carries
on with the original task.

Two deliberate asymmetries. Resume does **not** auto-start a stopped desktop, though everywhere
else "absent" means boot it — the whole value of the resume is the session the human just
authenticated, and an ACI restart takes the filesystem with it, so it stops and says the login is
gone rather than working confidently on a signed-out browser. And the system prompt is rebuilt on
resume rather than replayed from storage, so a parked run picks up prompt fixes.

### The `done` event

The final text is on **`message`**, not `content` — that is what `orchestrator.py` writes. `@nesqbot/protocol` exports `doneEventText(data)`, which reads either; clients should use it rather than reaching for a field name.

Two distinct frames share the `done` name, and clients must tell them apart:

- **finished turn** — `{message_id, bot_id, bot_name, message, tier, cost_usd, approval_id}`
- **stream close-out** — `{thread_id, reason: "closed"}`, emitted from both endpoints' `finally` when a connection ends without a terminal event. Use `isStreamClosedDone()`; do not coerce a missing `message_id` into `""`, which mints a bogus message.

`tier` is **`null`** on a budget-blocked turn, since no model was called. Do not coerce it to `""`.

Both event unions also carry an `unknown` arm (`{event: "unknown", name, data}`). The protocol parsers never return `null` and never throw — unrecognised names, bad JSON, and failed validation all route there with the raw data preserved, so adding a server event cannot break a deployed client.

## Runs / audit

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/runs?thread_id=&bot_id=&status=&limit=` | `RunOut` list |
| GET | `/runs/{run_id}` | single |
| POST | `/runs/{run_id}/resume` | Continue a run parked in `awaiting_human`. Body `ResumeRunIn {note?}` → `ResumeRunOut`. This is the "I've finished, continue" button: the agent drove until it hit a login it cannot pass, handed you the screen, and this resumes **the same task** — rebuilding the conversation, replaying the step record, taking a fresh screenshot to see what you did, and carrying on. Owner-scoped (404, never 403). Idempotent via a conditional status update, so a double-click loses the race and returns `resumed: false` rather than starting a second loop. A run with no agent state is 409 `run_not_resumable` |
| POST | `/runs/{run_id}/cancel` | Abandon a run that will not finish: `CancelRunIn {reason?}` → `CancelRunOut {ok, cancelled, run_id, status, detail?}`. The escape hatch, and it exists because there was none — a run whose process died mid-step stays `running` for ever (nothing reconciles it, and the API restarts on every deploy), and a run parked in `awaiting_approval` whose approval was already decided waits on something that no longer exists. Both leave the UI showing work that will never progress. Accepts `queued`, `running`, `awaiting_human` and `awaiting_approval`; deliberately permissive, because a cancel button that refuses is the same dead end with a different message. Idempotent like `resume` — a second press is `cancelled: false` with the status the run actually has, not an error. Writes a `run_cancelled` audit event naming the person. Owner-scoped (404, never 403) |
| POST | `/runs/{run_id}/status` | worker callback: `{status, error?, detail?, routine_id?, thread_id?, bot_id?, workflow_id?}` → updates `Run.status`/`error`/`finished_at`, stores `detail` on the run, writes an `AuditEvent`. This is how routine failures become visible in the UI. Returns `RunOut` |
| GET | `/audit?bot_id=&event_type=&limit=&before=` | `AuditEventOut` list, newest first |

## Approvals

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/approvals?status=pending&bot_id=` | list |
| POST | `/approvals` | create directly: `{bot_id, run_id?, risk, title, summary, payload}` using the held-payload shape below → `ApprovalOut`. Used by routine steps of `type: "approval"` |
| GET | `/approvals/{id}` | single |
| POST | `/approvals/{id}/decide` | `{decision: approved\|rejected, note?}`. On `approved` the API MUST execute the held action via `services.approvals.execute_approved(...)` and return `ApprovalOut` plus `execution` `{ok, result\|error}`. Deciding a non-pending approval → 409. **Deciding also continues the task.** A run parked in `awaiting_approval` resumes through the same path as the takeover Continue button — same persisted state, same conversation rebuild, same conditional status claim, so a double-press is a no-op. The continuation rides back as `execution.continuation`, a superset of `{ok, result\|error}`, so no client breaks. The resumed model is told whether the approved action actually **ran** or was approved and then refused at execution; a rejection is reported as a person's decision, not to be retried or routed around |
| POST | `/approvals/{id}/expire` | mark expired (sweeper) |

### Standing permissions

"Don't ask me again for this button." A rule is learned **only** from a human's
explicit yes on an action that actually ran, and **only** for `send` — `spend` and
`delete` can never be learned, so money and destruction ask every time. Two origins:
the person wrote it in the approval note, or they approved the same control on the
same page three times with no refusal in between.

Matched by *identity*, not by name: role, accessible name, and scheme+host+path,
through the same `resolve_approved` path an approved action uses. A covered action
therefore gets a **stricter** proof than an attended one — one match, same page,
untruncated snapshot, no positional fallback — because nobody is watching it.

Applied **at** the gate, never around it: a hit rewrites the assessment to
`requires_approval=False` with a recorded reason and stamps `action_log.standing_approval_id`.
`simulation._execute` is unchanged and still unreachable except through `perform`.

**A scheduled routine inherits these grants**, because a routine run carries the
requester as its actor and the scope is "this element, this page, this bot, until
revoked". That is a real consequence of the chosen scope, not an oversight: a grant
made while watching applies later when nobody is.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/standing-approvals?include_revoked=` | Standing permissions this user has granted, newest first → `StandingApprovalListOut` `{items: StandingApprovalOut[], always_asks}`. Owner-scoped **by column**, never by bot visibility. Each item carries what it permits in plain words (`permits`, `place`, `element`) **and** its provenance (`origin` `note\|repetition`, `note`, `source_approval_ids`, `granted_at`, `used`, `last_used_at`). A rule that cannot say where it came from cannot be created — the database enforces it, not just the service |
| POST | `/standing-approvals/{id}/revoke` | Revoke one, effective on the next gate decision → `StandingApprovalOut`. Idempotent; the row is kept with `revoked_at`/`revoked_by` and never deleted, so the history of what was permitted survives the permission. Somebody else's rule → 404, never 403 |

Held payload shape, written by the orchestrator:

```json
{
  "kind": "connector_action",
  "connector_id": "microsoft_graph",
  "action": "send_mail",
  "input": {},
  "draft": "…",
  "thread_id": "…",
  "requested_by": "<user uuid>",
  "created_by": "<user uuid>"
}
```

`kind` is one of `connector_action`, `mcp_tool`, `desktop_steps`, `message_only`.

**One classifier.** Action risk is classified in exactly one place, `services/risk.py::classify_action_risk`
(`services/desktop.py` re-exports it for existing callers). Connector, desktop, MCP, routine-step,
chat-turn and plan execution all reach it through `services.simulation.perform`. A declared risk —
in a taught step, an `McpCallIn`, or a `DesktopActionIn` — is **escalate-only** on every path: it can
raise the classification, never lower it.

This is load-bearing rather than tidy. Earlier revisions of this codebase carried three independent
gate implementations, and a step named `send_invoice` gated or did not depending on which executor
ran it — so the control appeared present everywhere and was enforced nowhere in particular.

**Scoping keys.** Approvals are scoped by requester, not by bot — inheriting visibility from a shared system bot would leave every `send`/`spend`/`delete` approval decidable by any authenticated user.

- `requested_by` — the human on whose behalf the action was filed. Owner resolution precedence is `requested_by` → the thread owner behind `run_id` → the custom bot's `owner_user_id`. Stamped at creation so scoping survives thread deletion. Callers that know the human MUST set it; `POST /routines/{id}/run` and interactive turns always can.
- `created_by` — the identity that filed the approval, stamped only when it differs from the owner (i.e. the worker). Grants **read only**, so `wait_for_approval_activity` can poll; it is excluded from decide and expire.

An approval with neither key and no `run_id` has no knowable human. This is the genuine unattended case — a cron-triggered routine — and falls back to bot visibility with a logged warning. Populating `Routine.owner_user_id` at create/teach time is what makes those attributable.

## Connectors

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/integrations/connectors` | catalog |
| POST | `/integrations/connectors` | register custom |
| DELETE | `/integrations/connectors/{id}` | non-first-party only |
| GET | `/bots/{bot_id}/connectors` | bindings + status |
| POST | `/bots/{bot_id}/connectors/{connector_id}` | bind/update |
| DELETE | `/bots/{bot_id}/connectors/{connector_id}` | unbind |
| POST | `/bots/{bot_id}/connectors/{connector_id}/actions/{action}` | execute; when `requires_approval(risk)` → 201 `{approval_id, status:"pending_approval"}` instead of executing |

## MCP

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/integrations/mcp` | list |
| POST | `/integrations/mcp` | register |
| PATCH | `/integrations/mcp/{id}` | enable/disable, allowlist |
| DELETE | `/integrations/mcp/{id}` | |
| GET | `/integrations/mcp/{id}/tools` | live `tools/list`, mock when unreachable |
| POST | `/bots/{bot_id}/mcp/{mcp_id}` | attach |
| DELETE | `/bots/{bot_id}/mcp/{mcp_id}` | detach |
| POST | `/bots/{bot_id}/mcp/{mcp_id}/call` | `{tool, arguments, risk?}`. **Risk-gated**: the tool name is classified server-side and a gated call returns 201 `PendingApprovalOut` instead of executing. `risk` is escalate-only. Executes through `simulation.perform`, so it is also recorded in the action log |

## Bot Desktop

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/bots/{bot_id}/desktop` | state |
| POST | `/bots/{bot_id}/desktop/start` | lifecycle |
| POST | `/bots/{bot_id}/desktop/stop?wipe=` | lifecycle |
| POST | `/bots/{bot_id}/desktop/suspend` | pause |
| POST | `/bots/{bot_id}/desktop/resume` | unpause |
| POST | `/bots/{bot_id}/desktop/action` | Risk-gated; actions classified `send`/`spend`/`delete` return 201 `PendingApprovalOut` instead of running. Optional `risk` is escalate-only |
| GET | `/bots/{bot_id}/desktop/screenshot` | proxies sidecar `/screenshot`; mock mode returns a generated placeholder `{ok,width,height,png_base64}` |
| GET | `/bots/{bot_id}/desktop/windows` | proxies sidecar `/windows` |
| POST | `/bots/{bot_id}/desktop/stream/ticket` | Mint a short-lived stream ticket → `{ticket, expires_at, stream_url, websocket_path, vnc_password}` |
| GET | `/bots/{bot_id}/desktop/stream/{ticket}/{asset_path}` | noVNC assets, proxied from the desktop's private IP |

### Viewing a desktop

A Bot Desktop has **no public IP** — one hypervisor-isolated container group per bot on a
delegated subnet, which is the isolation claim this product is built on. `stream_url` is
therefore a `10.60.x.x` address that no client machine can route to, and pointing an iframe at it
produced "This content is blocked."

The API proxies the stream instead: it already sits inside the VNet, and it is the only thing
that should be able to reach a desktop.

Neither an `<iframe src>` nor a WebSocket handshake can carry an `Authorization` header, so both
legs authenticate with a **ticket**: `v1:{bot_id}:{user_id}:{exp}:{nonce}`, HMAC-SHA256 signed
under `JWT_SECRET`, 60-second TTL, minted by the authenticated `POST` above. It is signed rather
than stored so it verifies on any replica, and it lives in the **path** rather than the query
because noVNC fetches dozens of relative assets and relative resolution drops a query string.
The session JWT is never exposed. Authorization is re-checked on redeem, not just at mint, so a
ticket cannot outlive the access that produced it.

There is also a WebSocket route the contract guard cannot see (it inspects only
`starlette.routing.Route`), carrying the VNC transport itself:

```
WS  /api/bots/{bot_id}/desktop/stream/{ticket}/websockify
```

The control leg is **single-use** — a second socket on the same ticket is refused with close
`4401`. Asset fetches deliberately are not: noVNC keeps loading files after it connects, and
burning the ticket there would leave a half-painted page while protecting nothing but stock
noVNC files. Close codes: `4401` unauthorised, `4404` no desktop, `4409` ticket already redeemed,
`4502` upstream unreachable, `4408` idle timeout.

## Routines

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/routines?bot_id=` | list |
| POST | `/routines` | create |
| POST | `/routines/teach` | from recorded steps |
| GET | `/routines/{id}` | single |
| PATCH | `/routines/{id}` | name/desc/steps/cron/enabled → re-syncs Temporal schedule; bumps `version` when steps change |
| DELETE | `/routines/{id}` | also deletes the Temporal schedule |
| POST | `/routines/{id}/run` | start `RoutineWorkflow` now → `{workflow_id, run_id}`; inline fallback when Temporal unreachable |
| GET | `/routines/{id}/runs` | recent runs |

## Work items

A work item is an owned, transferable unit of work — the object a bot hands to another bot. It is
generalised with a `type` (`lead`, `ticket`, `invoice`, …) rather than modelled as a `leads` table:
the transfer ledger is the differentiator (`docs/competitive-analysis.md`), and a differentiator
wants one place to be queried from. Owner-scoped to the human via `work_items.owner_user_id`, which
never changes; only `owner_bot_id` moves. Not-yours is 404, never 403.

Every handover is a row in `work_item_transfers` — from bot, to bot, actor (the human, and the
initiating bot where one drove it), reason, source, timestamp. That table carries no foreign keys,
like `audit_events` and `action_log`, so deleting the work item does not erase the record that it
was handed over.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/work-items?type=&status=&owner_bot_id=&limit=` | `WorkItemOut` list, newest first, scoped to the calling user. `owner_bot_id` is the "what is this bot holding?" queue view |
| POST | `/work-items` | Create, already owned by a bot: `WorkItemIn {owner_bot_id, title, type?, summary?, status?, thread_id?, detail?, keys?, reason?}` → `WorkItemOut`. Writes the **opening ledger row** with `from_bot_id: null`, so "who has held this" has no gap at the front. `owner_bot_id` must be a bot the caller can see; `thread_id` must be a thread they own |
| GET | `/work-items/{work_item_id}` | Single, 404 if not the caller's |
| PATCH | `/work-items/{work_item_id}` | `UpdateWorkItemIn`: title/summary/status/resolution/thread_id/detail/keys. Sending `owner_bot_id` is **422**, not a silent drop — ownership moves only through `/transfer`, which is the path that writes the ledger, and a 200 that dropped the field would read as a successful handover with no record. Keyed on presence, so an explicit `null` is refused too. This is the one input model in the API that rejects unknown keys. `keys` replaces the whole set rather than merging. Setting `status: "closed"` stamps `closed_at`; moving off it clears the stamp |
| DELETE | `/work-items/{work_item_id}` | Deletes the item and its keys → `OkOut`. **The transfer ledger survives** — see above |
| POST | `/work-items/{work_item_id}/transfer` | Hand the item to another bot: `WorkItemTransferIn {to_bot_id, reason, actor_bot_id?, detail?}` → `WorkItemTransferResultOut {ok, transferred, work_item, transfer?, detail?}`. `reason` is **required** (`min_length=1`): a ledger of timestamps without reasons is no better than what the competitor does not have. Idempotent — transferring to the bot that already holds it returns `transferred: false` and writes no second ledger row, same shape and reasoning as `ResumeRunOut.resumed`. 409 `work_item_closed` on a closed item; 404 on a target bot the caller cannot see. Not risk-gated, by decision: a transfer reaches nothing outside the tenant and is undone by transferring back |
| GET | `/work-items/{work_item_id}/transfers` | `WorkItemTransferOut` list, newest first — the handover ledger. Reachability is checked against the work item, not the ledger rows |

`status` is one of `open` (nobody has touched it), `working` (the owning bot is acting), `waiting`
(blocked on the outside world — where a lead sits between outreach and reply), `closed` (terminal;
`resolution` carries the outcome).

`keys` are the external identities an inbound reply is recognised by — `[{channel, value}]`, e.g.
`{"channel": "email", "value": "sarah@acme.test"}`. Normalised (trimmed, lowercased) server-side on
both write and lookup. Deliberately **not** unique on `(channel, value)`: the same person can
honestly be two work items, and a unique index would surface that as an `IntegrityError` inside a
webhook and discard a real customer reply. `services.work_items.resolve_by_key` returns ordered
candidates instead — open before closed, then most recent `last_event_at`, then most recent
`created_at`.

Two timestamps that look similar and are not: `updated_at` moves on any edit; `last_event_at` moves
only when the **outside world** acted, which is what "the lead answered" means, and is what makes a
stalled-outreach sweep an indexed query.
### Work items an agent can reach

Inside an agent turn the same four verbs are tools rather than routes, dispatched by
`services/agent_work_items.py`. They are owner-scoped to the human the run answers to — the
*originating* human on a delegated chain — and answer "there is no work item with that id" to
anything else, never "forbidden".

| Tool | Notes |
| --- | --- |
| `create_work_item` | `{type, title, summary?, status?, detail?, keys?}`. Owned by the calling bot and pinned to the thread; writes the opening ledger row with `actor_bot_id` set. `keys` is `{channel: address}` rather than the API's `[{channel, value}]`. Existing records carrying the same address are **reported, not merged** |
| `find_work_items` | `{id?, key?, query?, status?}`. `key` matches the address across every channel (`resolve_by_value`), owner-scoped. Returns every candidate in `resolve_by_key`'s order and says so when there is more than one |
| `update_work_item` | `{id, status?, summary?, detail?, resolution?, keys?}`. `detail` **merges** and `keys` are **added** — the API replaces both, correctly, because an HTTP client holds the object it just read and a model does not. `owner_bot_id`/`to_slug` are refused, pointing at the transfer tool |
| `transfer_work_item` | `{id, to_slug, reason}`. Target must be a bot **on this thread** — narrower than the HTTP route's "any visible bot", because a model can only name bots it has been told about, and a handover to an absent owner is a handover to silence. Idempotent; refuses a closed item; not risk-gated, the same decision as the route |

`status` accepts only `open`/`working`/`waiting`/`closed`. A pipeline stage (`qualified`,
`messaged`, `quoting`) is **refused with the mapping and nothing is written**; the stage belongs in
`detail`, and "handed to sales" is a ledger row rather than a column. Refusing rather than coercing
matters: a 200 would leave the model believing its status exists while the row said something else.

Advertising is gated by `context_budget.ToolContext` — `create` always, `find` once the human has a
record, `update` once the model holds an id, `transfer` once there is also another bot on the
thread. That is **280 prompt tokens on a fresh tenant against 933 for all four**, and it is what
kept `AGENT_REQUEST_TOKEN_MEAN`/`_CEILING` passing without raising either. All four stay
*dispatchable* when not advertised, so a model reaching for one it was not offered gets a real
answer rather than an unknown-tool error.


## Inbound events

Step two of the cowork loop: the lead-gen bot sends, **a lead answers**, and the sales bot closes.
Two ways in — a signed webhook a provider pushes to, and a poll that pulls from a connector the
owner already bound — converging on one code path before any decision is made about the message,
so a reply that arrives by email is never handled differently from the same reply pulled out of a
mailbox. Inbound addresses resolve to a work item through `work_item_keys`, which is deliberately
not unique on `(channel, value)`: one candidate is acted on, several take the first and record the
rest, none becomes a row in a queue a person works. Reply text reaches the model only as fenced
`user` data inside a per-message random nonce, never as a system or tool message, and anything the
woken run then wants to do outside the tenant still goes through `simulation.perform` and the same
approval gate as everything else.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/inbound/sources?kind=&limit=` | `InboundSourceOut` list, newest first, scoped to the calling user |
| POST | `/inbound/sources` | Create a way in: `InboundSourceIn {name?, kind?, channel?, bot_id?, bot_ids?, secret_ref?, connector_id?, config?, enabled?}` → `InboundSourceOut`. `slug` cannot be supplied — it is the public path segment and is generated server-side from a CSPRNG, so a caller-chosen value would be both a name grab on a globally unique column and an enumerable surface. `kind: "webhook"` **requires** `secret_ref`, else 422 `signing_key_required`: an unsigned hook that starts agent runs is a way to spend the owner's budget. `secret_ref` must be a reference (`env://NAME`, `kv://vault/name`, or a bare name against `AZURE_KEY_VAULT_URL`), never the key — 422 `invalid_secret_ref` otherwise. `kind: "poll"` requires `bot_id` and a `connector_id` whose action is classified `observe`; 422 `poll_must_read` on anything else. `bot_id` and every `bot_ids` entry must be a bot the caller can see |
| PATCH | `/inbound/sources/{source_id}` | `UpdateInboundSourceIn`: name/channel/bot_id/bot_ids/secret_ref/connector_id/config/enabled → `InboundSourceOut`. Neither `slug` nor `kind` is editable — a hook URL that can be changed in place is one that can be changed onto a value another tenant is about to be given. Rejects unknown keys. `enabled: false` is the kill switch: a disabled source then refuses every delivery with the answer an unknown slug gets |
| DELETE | `/inbound/sources/{source_id}` | Deletes the source → `OkOut`. **`inbound_events` survives** — no foreign key, like `audit_events` and `work_item_transfers`, so deleting the hook does not erase what a customer said |
| POST | `/inbound/sources/{source_id}/poll` | Fetch from the bound connector now and treat every record as inbound → `InboundPollOut {ok, source_id, fetched, matched, ambiguous, unmatched, unroutable, duplicates, event_ids, detail?}`. The pull half of the ingress; everything after the fetch is the identical path a webhook takes. Informative counts, unlike the webhook, because the caller is authenticated and it is their own data. 409 `not_a_poll_source` on a webhook source, 409 `source_disabled` on a disabled one |
| GET | `/inbound/events?status=&work_item_id=&source_id=&limit=` | `InboundEventOut` list, newest first, scoped to the calling user. **This endpoint is the promise that nothing is silently discarded.** `?status=unmatched` is the queue of replies that resolved to no work item — a real person answering from a second address — and `?status=ambiguous` is the shorter list where an address matched several items, the first was taken and the rest are on the row. Scoped by `owner_user_id` stamped from the source rather than joined through the work item, which is what lets an unmatched event still belong to exactly one person. `subject` and `body` are attacker-authored: **render them as text** |
| POST | `/inbound/hooks/{source_slug}` | **Unauthenticated by nature** — the sender is a mail provider, not a user. Authenticated instead by `X-Nesq-Signature: v1=<hex>`, an HMAC-SHA256 over `v1:{timestamp}:{raw body}` with `X-Nesq-Timestamp` (unix seconds) inside the MAC, compared with `secrets.compare_digest`, within ±300s. Body: `{from\|sender\|address\|email, body\|text\|message, subject?, channel?, external_id\|id\|message_id?, meta?}`, max 256KB (enforced while reading, not from `Content-Length`), 60/min per slug+client. Answers **202 `InboundAckOut {ok, status:"accepted"}` — byte-identical whether the reply matched one work item, several, none, or was a replay**, so an unauthenticated caller cannot probe which addresses this tenant is working; the owner reads the real outcome at `GET /inbound/events`. Unknown slug, disabled source, unresolvable signing key, stale timestamp and wrong digest all answer 401 `invalid_signature` and all do the HMAC work, so no path is faster than another. 413 `payload_too_large`, 429 `rate_limited` with `Retry-After`. 400 `invalid_payload` is reachable only past the signature check. Replay is rejected by two unique indexes on `inbound_events`: the signature digest (a verbatim retry) and the provider's own message id (a re-signed retry) |

### How a reply reaches the right bot

`inbound._thread_for`, and the rule that matters is that **nothing is ever added to a thread on the
strength of what a message says**:

1. **The work item already has a thread** — use it, and seat nobody except the owning bot. Thread
   membership is the human's to decide. The owning bot is the one exception, and it is not the
   escalation that `_delegate_targets` guards against: both ends were set by an authenticated
   human, and without it `mention_bot_ids` filters to nothing and some other bot answers in its
   place.
2. **No thread yet** — create one owned by `work_items.owner_user_id`, seat the owning bot plus
   `inbound_sources.bot_ids`, and pin it to the item. **That roster is the product's answer to "how
   does a reply reach Sales":** a human named those bots ahead of time through an authenticated
   API, and every entry is re-checked for visibility at seat time, so a deleted bot is not seated.

This matters because thread membership *is* the delegation boundary — a threadless run can delegate
to nobody, by construction. The actor for the whole chain is `work_items.owner_user_id`; a work item
with no resolvable human gets no run at all and is recorded `unroutable`, never a placeholder actor.

## Rehearsal and reversibility

Answers the two gaps that make agent products unfit for customer-facing work: you cannot
rehearse an action, and you cannot take it back. See `docs/competitive-analysis.md`.

A dry run performs **no** side effects. Every outbound effect in the service layer passes
through one chokepoint, `services.simulation.perform`, which either records the intent or
performs it and writes the undo-log entry — the real and simulated paths share a single
traversal, so a plan cannot drift from what actually executes.

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/routines/{routine_id}/dry-run` | Rehearse a routine → `PlanOut`. No side effects |
| POST | `/bots/{bot_id}/connectors/{connector_id}/actions/{action}/dry-run` | Rehearse one action → `PlanOut` |
| POST | `/plans` | Save a plan. Takes the *inputs* to a rehearsal (`routine_id`, or `bot_id` + `steps`) and re-runs it server-side rather than accepting a client-supplied plan — otherwise the drift check would be validating the client's own arithmetic. Optional `expected_content_hash` asserts the plan a human was shown; a mismatch is 409 `plan_drifted` |
| GET | `/plans?bot_id=&limit=` | List saved plans |
| GET | `/plans/{plan_id}` | Single plan |
| POST | `/plans/{plan_id}/execute` | Execute exactly the approved plan → `RoutineRunOut`. **409 `plan_drifted`** if the underlying routine changed since the plan was produced, **409 `already_executed`**, 404 `routine_gone`. Approving a plan does not pre-approve its gated steps — a `send` inside an approved plan still holds for a human |
| GET | `/action-log?bot_id=&run_id=&reversible_only=&limit=` | Executed effects with their reversibility |
| POST | `/action-log/{action_log_id}/undo` | Run the compensating action. 409 `already_undone`, 422 `not_reversible` with the reason, 404 if not visible |
| GET | `/reversibility` | The reversibility matrix — what can and cannot be taken back. Product documentation, deliberately not tenant-scoped |

**Honesty is the contract.** A compensator is recorded only where a real inverse exists:
`draft_reply` deletes the draft, `create_task` deletes the task, `crm.update_fields` restores
values captured *before* the write. A sent email and a delivered ticket reply are irreversible
and are recorded `reversible=False` with the reason; so are desktop steps and MCP tool calls,
where no inverse is knowable. A compensator that silently no-ops would turn a known limitation
into a false promise, so there are none.

Plans and action-log rows are scoped like approvals — the author wins, and an unattributed row
falls back to bot visibility — because both can hang off a *shared* system bot, and bot
visibility alone would publish one user's rehearsal, and the right to execute it, tenant-wide.

## Memory / KB

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/bots/{bot_id}/memories?limit=` | list |
| POST | `/bots/{bot_id}/memories` | `{kind, content}` → embeds |
| DELETE | `/memories/{id}` | |
| GET | `/kb?q=&limit=` | vector search when embeddings available, keyword fallback |
| POST | `/kb` | `{title, body}` → embeds |
| PATCH | `/kb/{id}` | update, re-embed |
| DELETE | `/kb/{id}` | |

## Usage / evals

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/usage?days=1` | per-bot spend + entries |
| PATCH | `/bots/{bot_id}/budget` | `{daily_budget_usd}` |
| POST | `/evals/run` | single case |
| POST | `/evals/suite` | `{cases:[EvalCaseIn]}` → `{passed, total, results[]}` |

## Service-layer contracts (Python)

`app/services/approvals.py`

```python
async def execute_approved(db, approval: Approval, user: User) -> dict   # {"ok": bool, ...}
async def create_approval(db, *, run_id, bot_id, risk, title, summary, payload) -> Approval
```

`app/services/rag.py`

```python
async def embed(texts: list[str]) -> list[list[float]] | None    # None when Azure unconfigured
async def upsert_memory_embedding(db, memory: Memory) -> None
async def upsert_kb_embedding(db, article: KbArticle) -> None
async def search_kb(db, query: str, limit: int = 5) -> list[tuple[KbArticle, float]]
async def search_memories(db, bot_id, user_id, query: str, limit: int = 8) -> list[Memory]
```

`app/services/events.py`

```python
async def publish(channel: str, event: str, data: dict) -> None   # redis pubsub, in-proc fallback
async def subscribe(channel: str) -> AsyncIterator[tuple[str, dict]]
def thread_channel(thread_id) -> str                              # f"thread:{thread_id}"
```

`app/services/model_router.py` additions

```python
async def stream_chat(*, task, messages, tools=None, fail_count=0) -> AsyncIterator[str]
# yields content deltas; the final ChatResult is exposed as `.last_result` once exhausted
```

Retries via tenacity: 3 attempts, exponential backoff, only on `APIConnectionError` / `RateLimitError` / 5xx. 60 s request timeout.

`app/services/desktop.py` additions — methods on `DesktopManager`, not module-level functions

```python
class DesktopManager:
    async def resume(self, db, bot_id) -> BotDesktop
    async def screenshot(self, db, bot_id) -> dict    # {ok,width,height,png_base64}
    async def windows(self, db, bot_id) -> dict
```

`app/services/temporal_client.py` (new)

```python
async def get_client() -> Client | None                  # None when unreachable
async def sync_routine_schedule(routine) -> str | None   # returns schedule id
async def delete_routine_schedule(routine_id) -> None
async def start_routine_now(routine, user_id: str | None = None) -> dict   # {workflow_id, run_id}
```

`user_id` is the human who *triggered this run*, which differs from the routine's owner whenever someone runs a colleague's routine — and that is the case where attribution matters most, since the resulting approval should be decidable by whoever pressed the button. It wins over the owner-derived value; absent, `routine_argument` falls back to `routine.owner_user_id`. Returns `{}` on failure, not a truthy error dict, so the caller's inline fallback triggers correctly. `sync_routine_schedule` takes no override — a cron-fired schedule has no triggering human by definition.

## Error shape

Handled errors return `{"detail": "…", "code": "snake_case_code"}` with the appropriate status.
Unhandled errors return 500 `{"detail":"internal_error","code":"internal_error","request_id":"…"}` and are logged with the id issued by the `X-Request-Id` middleware.
