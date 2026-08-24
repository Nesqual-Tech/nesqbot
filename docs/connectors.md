# Connectors

A connector is a named set of actions a bot can call. Each action carries a
**risk class**, and that class — not the bot, not the prompt — decides whether
the action runs or stops at a human.

First-party connectors ship with the API (`apps/api/app/services/connectors.py`):

| Connector         | Auth    | Actions                                                                       |
| ----------------- | ------- | ----------------------------------------------------------------------------- |
| `microsoft_graph` | oauth2  | `list_inbox` (observe), `draft_reply` (draft), `send_mail` (**send**)         |
| `crm`             | oauth2  | `search_accounts` (observe), `update_fields` (mutate), `create_task` (mutate) |
| `ticketing`       | api_key | `list_open` (observe), `draft_reply` (draft), `send_reply` (**send**)         |

Custom connectors are registered at runtime through
`POST /api/integrations/connectors`, and are best authored with
`@nesqbot/connector-sdk` so the manifest is validated before it is sent.

## Manifest shape

```json
{
  "id": "my_tool",
  "name": "My Tool",
  "version": "1.0.0",
  "auth": "oauth2",
  "scopes": ["read"],
  "actions": [
    {
      "name": "list_items",
      "description": "List items",
      "risk": "observe",
      "input_schema": { "type": "object" }
    }
  ],
  "risk_default": "observe",
  "first_party": false
}
```

| Field                    | Rule                                                                                                                       |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `id`                     | `lower_snake_case`, 2–64 chars, starts with a letter. It is the primary key and a URL segment — it cannot be changed later |
| `name`                   | Human label shown in the integrations list                                                                                 |
| `version`                | `major.minor.patch`                                                                                                        |
| `auth`                   | `oauth2` \| `api_key` \| `none`                                                                                            |
| `scopes`                 | Strings, informational — displayed at bind time so the human knows what they are consenting to                             |
| `actions[].name`         | `lower_snake_case`, unique within the connector. Appears in audit rows and approval payloads                               |
| `actions[].description`  | Required and non-empty: this text is what a human reads on the approval card at 11pm                                       |
| `actions[].risk`         | One of the six risk classes below                                                                                          |
| `actions[].input_schema` | JSON Schema, `type: "object"`                                                                                              |
| `risk_default`           | Fallback for actions that omit `risk`                                                                                      |
| `first_party`            | Always `false` for custom connectors. First-party connectors cannot be deleted through the API                             |

### Making real calls

Everything above describes a connector. The keys below describe how to *call*
it, and they are all optional: a manifest without them registers exactly as it
always did, and its actions return the mock payloads they always did.

```jsonc
{
  "id": "invoice_portal",
  "auth": "api_key",
  "base_url": "https://invoices.internal/api", // ← turns the mock into a call
  "api_key_header": "X-Api-Key",
  "actions": [
    {
      "name": "list_unpaid",
      "description": "List unpaid invoices",
      "risk": "observe",
      "input_schema": {
        "type": "object",
        "properties": { "older_than_days": { "type": "integer" } },
      },
      "method": "GET",
      "path": "/invoices",
      "query": { "older_than": "{older_than_days}", "state": "unpaid" },
    },
    {
      "name": "draft_reminder",
      "description": "Draft a payment reminder",
      "risk": "draft",
      "input_schema": {
        "type": "object",
        "properties": { "invoice_id": { "type": "string" }, "fields": { "type": "object" } },
        "required": ["invoice_id"],
      },
      "method": "POST",
      "path": "/invoices/{invoice_id}/reminders",
      "body": { "note": "Reminder for {invoice_id}", "fields": "{fields}", "silent": false },
    },
  ],
}
```

| Field               | Rule                                                                                                                                                            |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base_url`          | Absolute origin, optionally with a path prefix. **Absent means this connector mocks** — that is the switch, per connector                                       |
| `api_key_header`    | Header the credential is sent in when `auth` is `api_key`. Defaults to `X-API-Key`                                                                              |
| `actions[].method`  | HTTP method. Defaults to `GET`                                                                                                                                  |
| `actions[].path`    | Path appended to `base_url`. **An action without a `path` mocks**, even when the connector has a `base_url`                                                     |
| `actions[].body`    | JSON body template. Objects and arrays are walked                                                                                                               |
| `actions[].query`   | Query-string template. Values that substitute to `null` are dropped                                                                                             |

`{placeholder}` in a path, query value or body string is replaced with the
input of that name — the same input the action's `input_schema` validated, so a
`required` key is guaranteed to be there. Path values are URL-encoded. A string
that is *exactly* one placeholder keeps the input's own type, so
`"fields": "{fields}"` sends the object rather than its text form. A template
naming an input that was not supplied returns
`{"ok": false, "error": "…references input 'x', which was not supplied"}`
without sending anything.

Auth follows the manifest's existing `auth` field:

| `auth`    | What is sent                                          |
| --------- | ----------------------------------------------------- |
| `oauth2`  | `Authorization: Bearer <resolved secret>`             |
| `api_key` | `<api_key_header>: <resolved secret>`                 |
| `none`    | Nothing. The credential is not sent at all            |

The reply is the result: a JSON object or array is passed through as-is,
anything else is wrapped as `{"status": …, "body": …}`.

## The risk model

```
observe  →  draft  →  mutate  ‖  send  →  spend  →  delete
        safe to run alone      ‖    always needs a human
```

| Class     | Means                                          | Approval |
| --------- | ---------------------------------------------- | -------- |
| `observe` | Reads data. Nothing changes.                   | no       |
| `draft`   | Prepares something for review. Nothing leaves. | no       |
| `mutate`  | Changes internal records.                      | no       |
| `send`    | Sends something outside the company.           | **yes**  |
| `spend`   | Commits money.                                 | **yes**  |
| `delete`  | Destroys data.                                 | **yes**  |

The split is deliberate and load-bearing: the three safe classes are the ones
whose worst case is an internal mess, the three gated classes are the ones
whose worst case reaches a customer, a bank account, or a backup that no longer
exists. `requiresApproval(risk)` in `@nesqbot/protocol` is the single
implementation; the API, the SDK and both clients import it rather than
re-listing the classes.

Classify honestly. A `mutate` label on something that emails a customer is not
a shortcut, it is a hole. When in doubt, escalate a class — the cost of an
unnecessary approval is one tap.

`@nesqbot/ui` carries matching colour roles (`getRiskColor(risk, scheme)`),
labels and one-line descriptions, so a risk chip looks the same everywhere.

## The approval flow

When a bot reaches a gated action:

1. The orchestrator does **not** execute it. It serialises everything needed to
   run it later into `Approval.payload`, sets the run to `awaiting_approval`,
   and emits an `approval` SSE event.
2. The client shows the approval. On mobile it is the whole point of the app.
3. `POST /api/approvals/{id}/decide` with `{"decision":"approved"}` replays the
   payload through `services.approvals.execute_approved(...)`. The response is
   the updated approval plus `execution: {ok, result|error}`.
4. `{"decision":"rejected"}` closes the run without executing. Deciding an
   approval that is not `pending` answers **409**.
5. Unattended approvals are swept to `expired`.

Calling a gated action directly —
`POST /bots/{bot_id}/connectors/{connector_id}/actions/{action}` — does not
bypass this. It answers `201 {approval_id, status: "pending_approval"}`
instead of executing.

Payloads are a discriminated union on `kind`
(`ApprovalPayload` in `@nesqbot/protocol`):

```jsonc
{
  "kind": "connector_action",
  "connector_id": "microsoft_graph",
  "action": "send_mail",
  "input": { "to": "…", "subject": "…", "body": "…" },
  "draft": "Hi Anna — following up on…", // render this, not the JSON
  "thread_id": "…",
}
```

The other three kinds are `mcp_tool` (`mcp_id` + `tool` + `arguments`),
`desktop_steps` (a recorded step list) and `message_only` (a draft with nothing
to execute). Narrow with `parseApprovalPayload()`; it returns `null` for legacy
rows so the UI can fall back to `Approval.summary`.

## Secret binding

Secrets never travel through the API's public surface and are never stored on
the binding row.

```
POST /api/bots/{bot_id}/connectors/{connector_id}
{ "secret_ref": "kv://nesqbot-kv/graph-sales-oauth", "status": "connected" }
```

- `bot_connectors.secret_ref` holds a **reference**, not a value. The row is
  safe to log, dump, and show in the UI. Three forms are accepted
  (`app/services/secrets.py`):

  | Form                        | Resolves to                                      |
  | --------------------------- | ------------------------------------------------ |
  | `kv://my-vault/graph-oauth` | that secret in that vault                        |
  | `graph-oauth`               | that secret in `AZURE_KEY_VAULT_URL`             |
  | `env://GRAPH_OAUTH`         | an environment variable — local development only |

- At execution time the API resolves the reference through
  `AZURE_KEY_VAULT_URL` with the container app's managed identity and hands the
  material to the connector as `ConnectorContext.secrets`. It stays in memory
  for the duration of the call.
- Bindings are per bot, not per user. Two bots can use the same connector with
  different credentials — that is how Sales and Support share `ticketing`
  without sharing an inbox.
- Rotating a credential means writing a new Key Vault version. The
  `secret_ref` does not change and no bot needs rebinding.
- `DELETE /api/bots/{bot_id}/connectors/{connector_id}` unbinds. It does not
  delete the secret; do that in Key Vault.

Locally, with no `AZURE_KEY_VAULT_URL` set, resolution returns empty secrets
and connector calls run in their mock paths.

## Execution: mock or real

Every connector action has two implementations — the mock in
`services/connectors.py` and a driver in `services/vendors/` — and the mock's
payload shape is the contract. A driver's job is to map the vendor's response
onto it, so the orchestrator, the approval card and the clients read the same
keys either way. `execute_connector_action` adds `"mock": true` when the mock
answered, and nothing else about the response changes.

The real call runs only when **all three** hold:

1. a credential resolved for this bot/connector binding;
2. `CONNECTOR_LIVE_CALLS` is not `false` — the deployment-wide kill switch;
3. the connector's driver has somewhere to call **and** knows this action.

Anything else mocks. Which path ran, and which of the three conditions decided
it, is logged at `DEBUG` by `app.services.connectors`.

| Connector         | Driver                          | Configured by                                             |
| ----------------- | ------------------------------- | --------------------------------------------------------- |
| `microsoft_graph` | `vendors/graph.py`              | `GRAPH_API_BASE_URL` (e.g. `https://graph.microsoft.com/v1.0`), or a `base_url` in its manifest |
| `crm`             | `vendors/crm.py` → generic HTTP | a `base_url` in its manifest — there is no CRM vendor behind this connector       |
| `ticketing`       | `vendors/ticketing.py` → generic HTTP | a `base_url` in its manifest — likewise a placeholder                       |
| anything custom   | `vendors/generic_http.py`       | the manifest keys above                                     |

`microsoft_graph` is the one first-party connector with a real vendor API:
`list_inbox` is `GET /me/messages?$top=`, `draft_reply` is
`POST /me/messages/{id}/createReply`, `send_mail` is `POST /me/sendMail`, and
the resolved secret is sent as an OAuth2 bearer token. Graph's fields are
mapped onto the mock's (`bodyPreview` → `snippet`,
`from.emailAddress.address` → `from`, and so on). `crm` and `ticketing` have no
vendor behind them, so nothing is invented for them: give their manifest a
`base_url` pointing at the CRM or help desk the deployment actually runs and
they go through the same manifest-driven path as a custom connector.

Failures never reach the caller as exceptions. A connection error, a 4xx or a
5xx all come back as `{"ok": false, "error": …}` (plus `"status"` when there
was one). Connection errors and 5xx are retried three times with exponential
backoff — the same policy as the model router — and `REQUEST_TIMEOUT_SECONDS`
bounds each attempt.

The resolved credential goes into the request headers and comes back out of
nothing: not the result, not the error string, not an audit event, not a log
line. Vendor error bodies and transport errors are scrubbed of it before they
are quoted, because a rejecting gateway will happily echo the header it
rejected. `tests/services/test_vendors.py` enforces this twice — a static audit
over `app/services/vendors/` and `app/services/connectors.py` that fails if a
credential-bearing name reaches a `return`, a logger, a `print` or a `raise`,
and runtime tests that feed a sentinel token through every failure path.

## Authoring a custom connector

`@nesqbot/connector-sdk` gives you the manifest types, a builder that refuses
to produce an invalid manifest, and a validator that returns structured errors
you can attach to form fields.

```ts
import {
  defineConnector,
  validateManifest,
  toRegisterRequest,
  requiresApproval,
  actionRisk,
} from "@nesqbot/connector-sdk"

export const invoiceTool = defineConnector({
  id: "invoice_portal",
  name: "Invoice Portal",
  version: "1.0.0",
  auth: "api_key",
  scopes: ["invoices.read", "invoices.pay"],
  risk_default: "observe",
  actions: [
    {
      name: "list_unpaid",
      description: "List unpaid invoices older than N days",
      risk: "observe",
      input_schema: {
        type: "object",
        properties: { older_than_days: { type: "integer", minimum: 0 } },
      },
    },
    {
      name: "draft_reminder",
      description: "Draft a payment reminder for an invoice",
      risk: "draft",
      input_schema: {
        type: "object",
        properties: { invoice_id: { type: "string" } },
        required: ["invoice_id"],
      },
    },
    {
      name: "pay_invoice",
      description: "Pay an invoice from the operating account",
      risk: "spend", // → always stops at a human
      input_schema: {
        type: "object",
        properties: { invoice_id: { type: "string" }, amount_usd: { type: "number" } },
        required: ["invoice_id", "amount_usd"],
      },
    },
  ],

  async execute(action, input, ctx) {
    const apiKey = ctx.secrets["api_key"]
    if (!apiKey) throw new Error("invoice_portal is not bound to a secret")

    switch (action) {
      case "list_unpaid": {
        const res = await fetch(`https://invoices.internal/api/unpaid`, {
          headers: { Authorization: `Bearer ${apiKey}` },
        })
        return res.json()
      }
      case "draft_reminder":
        return { draft: `Reminder for invoice ${String(input["invoice_id"])}` }
      case "pay_invoice":
        // Only ever reached after an approval was granted.
        return { ok: true, paid: input["invoice_id"] }
      default:
        throw new Error(`unknown action ${action}`)
    }
  },
})

// true — the SDK and the API agree, because both call the same function
requiresApproval(actionRisk(invoiceTool.manifest, "pay_invoice"))
```

`defineConnector` throws `ConnectorManifestError` on a malformed manifest —
a typo in a risk class is a governance hole, so it fails at author time rather
than at approval time. If you are building a manifest from user input instead,
validate without throwing:

```ts
const result = validateManifest(formValue)
if (!result.ok) {
  // [{ path: "actions[2].risk", code: "invalid_value", message: "risk must be one of …" }]
  setFieldErrors(result.errors)
} else {
  await api.post("/integrations/connectors", toRegisterRequest(result.manifest))
}
```

Then bind it to a bot and give it a credential:

```bash
curl -X POST localhost:8080/api/bots/$BOT/connectors/invoice_portal \
  -H 'X-Nesq-Dev: 1' -H 'Content-Type: application/json' \
  -d '{"secret_ref":"kv://nesqbot-kv/invoice-portal-key","status":"connected"}'
```

The execution body itself lives in the API — `@nesqbot/connector-sdk` is the
authoring and validation half, and does no I/O. Register the manifest through
the API, and implement the handler where the credentials are.

## MCP servers

MCP is the other extensibility path: register a server
(`POST /api/integrations/mcp`), allowlist its tools, attach it to a bot, and
call `POST /api/bots/{bot_id}/mcp/{mcp_id}/call`.

Differences from connectors worth knowing:

- Tools are discovered live (`GET /integrations/mcp/{id}/tools`), so the risk
  class cannot come from a manifest you authored. Treat MCP tools as at least
  `mutate`, and keep `tool_allowlist` tight — an empty allowlist means nothing
  is callable, which is the safe default.
- An approved MCP call is held as an `mcp_tool` payload, and executes through
  the same approval machinery.
- MCP servers are owned by the user who registered them
  (`mcp_servers.owner_user_id`).
