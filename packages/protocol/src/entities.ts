/**
 * Persisted entities as they appear on the wire.
 *
 * Field names are snake_case because these are verbatim FastAPI/pydantic
 * response bodies (`apps/api/app/schemas.py`). Do not camelCase them here —
 * the mapping, if any app wants one, belongs in that app.
 */

import type {
  ApprovalStatus,
  BotDesktopState,
  ConnectorAuthKind,
  ConnectorBindingStatus,
  DesktopProfile,
  InboundEventStatus,
  InboundSourceKind,
  IsoDateTime,
  McpTransport,
  MessageRole,
  ModelProvider,
  ModelTier,
  RiskClass,
  RunStatus,
  Uuid,
  WorkItemStatus,
} from "./core"

/* ------------------------------------------------------------------ *
 * Identity
 * ------------------------------------------------------------------ */

export interface User {
  id: string
  email: string
  display_name: string
  entra_oid?: string
}

/* ------------------------------------------------------------------ *
 * Bots
 * ------------------------------------------------------------------ */

export interface Bot {
  id: string
  slug: string
  name: string
  role: string
  /**
   * Write-only in practice. `BotOut` does not return it, so it is absent on
   * every read — `GET /bots`, `GET /bots/{id}`, and the bodies echoed back
   * by create and update. It is required going *in*
   * (`CreateCustomBotRequest.system_prompt`) and optional on
   * `UpdateBotRequest`.
   *
   * There is no "summary vs detail" split here because the API has no detail
   * endpoint: the list and the single-bot route return the identical shape.
   * A `BotSummary` type would invent a distinction the API does not make.
   */
  system_prompt?: string
  is_system: boolean
  daily_budget_usd: number
  desktop_profile: DesktopProfile
  /**
   * Both set pins this bot to one provider/model for every task it runs,
   * bypassing tier routing; both null (the default for every bot created
   * before this field existed) means tier routing decides. Never one without
   * the other — the API rejects that combination. See
   * `services.model_router.ModelRouter.chat`'s `bot=` parameter.
   */
  model_provider: ModelProvider | null
  model_name: string | null
  created_at: string
  /** Null for system bots; set for custom bots created through `POST /bots`. */
  owner_user_id?: Uuid | null
}

/* ------------------------------------------------------------------ *
 * Threads & messages
 * ------------------------------------------------------------------ */

export interface Thread {
  id: string
  title: string
  bot_ids: string[]
  created_at: string
  updated_at: string
}

export interface Message {
  id: string
  thread_id: string
  bot_id?: string | null
  user_id?: string | null
  role: MessageRole
  content: string
  created_at: string
  /** Free-form orchestrator annotations (tier, tool calls, handoff notes). */
  meta?: Record<string, unknown>
}

/* ------------------------------------------------------------------ *
 * Runs & audit
 * ------------------------------------------------------------------ */

/** One bot turn or routine execution. `GET /runs`, `GET /runs/{run_id}`. */
export interface Run {
  id: Uuid
  /**
   * Null for a routine run — a scheduled routine executes against a bot, not
   * a chat thread. Anything that groups runs by conversation has to skip
   * these rather than assume a thread exists.
   */
  thread_id: Uuid | null
  bot_id: Uuid
  /**
   * Set when the run was started by a routine. Indexed, and `ON DELETE SET
   * NULL` — so a run outlives the routine that scheduled it and this goes
   * null rather than the row disappearing.
   */
  routine_id?: Uuid | null
  status: RunStatus
  /** Set when the run is driven by Temporal rather than inline in the API. */
  temporal_workflow_id?: string | null
  /** Shared scratch space handed between bots during a handoff. */
  context_ledger: Record<string, unknown>
  /** Free-form progress detail written by the orchestrator or the worker. */
  detail?: Record<string, unknown> | null
  /** Set when `status` is `failed`. */
  error?: string | null
  finished_at?: IsoDateTime | null
  created_at: IsoDateTime
  updated_at?: IsoDateTime | null
}

/**
 * Known audit event types. The union stays open (`string & {}`) because the
 * API adds new ones without a client release.
 */
/**
 * The strings `apps/api` actually writes into `audit_events.event_type` —
 * grepped from `event_type="..."` across the API source, not designed ahead
 * of it. The previous version of this union used dot-separated names
 * (`"bot.created"`) that the API has never written; every consumer of this
 * type kept working anyway because of the `(string & {})` escape hatch below,
 * which is exactly why the drift went unnoticed. Kept alphabetical so a scan
 * for "is X here" is fast, not grouped by feature — grouping is how the
 * previous version silently dropped a third of these.
 */
export type AuditEventType =
  | "action_held_for_approval"
  | "action_undone"
  | "approval_created"
  | "approval_decision"
  | "approval_executed"
  | "approval_expired"
  | "bot_delegation"
  | "bot_delegation_finished"
  | "bot_delegation_refused"
  | "bot_deleted"
  | "bot_updated"
  | "budget_updated"
  | "chat_turn"
  | "connector_action"
  | "connector_deleted"
  | "connector_registered"
  | "connector_unbound"
  | "desktop_action"
  | "desktop_action_held"
  | "human_takeover_requested"
  | "inbound_event"
  | "inbound_source_created"
  | "inbound_source_deleted"
  | "inbound_source_updated"
  | "inbound_wake"
  | "kb_article_created"
  | "kb_article_deleted"
  | "mcp_call_held"
  | "mcp_deleted"
  | "mcp_tool_call"
  | "mcp_updated"
  | "run_cancelled"
  | "run_interrupted"
  | "run_parked_for_approval"
  | "run_status"
  | "routine_deleted"
  | "routine_run"
  | "routine_started"
  | "routine_started_inline"
  | "routine_taught"
  | "routine_updated"
  | "standing_approval_applied"
  | "standing_approval_granted"
  | "standing_approval_revoked"
  | "thread_deleted"
  | "work_item_created"
  | "work_item_deleted"
  | "work_item_transferred"
  | "work_item_updated"
  | (string & {})

/** `GET /audit`. Newest first. */
export interface AuditEvent {
  id: Uuid
  actor_user_id?: Uuid | null
  bot_id?: Uuid | null
  event_type: AuditEventType
  detail: Record<string, unknown>
  created_at: IsoDateTime
}

/* ------------------------------------------------------------------ *
 * Approvals
 * ------------------------------------------------------------------ */

/**
 * What happened to the task after the decision.
 *
 * Deciding an approval is one step of a task, not the end of it: the run that
 * asked is parked with everything needed to carry on, and both answers are
 * answers. The API continues it through the same conditional status claim the
 * takeover Continue button uses, and reports the result here.
 *
 * `continued: false` is **not** a failure. It is what a double-press, or a
 * decision racing the expiry sweeper, looks like — the run had already moved
 * on, and no second loop was started on the same browser session.
 */
export interface ApprovalContinuation {
  /** Whether this decision is what actually restarted the run. */
  continued: boolean
  run_id: Uuid
  /** The run's status now — `running`, `completed`, or `gone`. */
  status?: string | null
  /** How the resumed agent loop ended. */
  outcome?: string | null
  /** The reply the resumed run produced, if it produced one. */
  message_id?: Uuid | null
  /** Set when the continuation itself failed. The decision still stands. */
  error?: string | null
}

/**
 * Outcome of replaying a held payload, plus what the task did next.
 *
 * Three arms, because the API genuinely produces three shapes:
 *
 * 1. **approved and ran** — `ok: true`, with whatever the action returned.
 * 2. **approved and refused at execution** — `ok: false` with the reason. An
 *    approved DOM click is re-resolved against the page as it is *now* and can
 *    honestly refuse: the element is gone, two now match, the tab navigated.
 *    "Approved" and "ran" are two different facts and this union keeps them so.
 * 3. **rejected, but the task carried on** — no `ok` at all, because nothing was
 *    executed; only `continuation` is present. This arm was missing while the
 *    API was already emitting it, and a client branching on
 *    `execution.ok ? … : "failed"` renders a perfectly ordinary rejection as an
 *    execution failure. Branch on `ok === true` / `ok === false`, or use
 *    `approvalExecutionOutcome()`.
 */
/**
 * What every arm of {@link ApprovalExecution} may carry, whichever way it went.
 *
 * `standing_*` is present only on the decision that *created* a standing
 * permission, and it is on the decision's own response rather than only in the
 * chat reply for one reason: not every approval has a parked run to carry a
 * sentence into a reply. A routine-created hold has none, and "announced" must
 * not mean "announced when the architecture happens to allow it".
 */
interface ApprovalExecutionExtras {
  continuation?: ApprovalContinuation
  /** The permission this decision granted. Absent on every other decision. */
  standing_approval?: StandingApproval | null
  /** The sentence to show. Composed server-side; render it, do not rebuild it. */
  standing_announcement?: string | null
}

export type ApprovalExecution =
  | ({ ok: true; result?: unknown } & ApprovalExecutionExtras)
  | ({ ok: false; error: string } & ApprovalExecutionExtras)
  | ({
      ok?: undefined
      result?: undefined
      error?: undefined
      continuation: ApprovalContinuation
    } & ApprovalExecutionExtras)

/** Which of the three {@link ApprovalExecution} arms this is. */
export type ApprovalExecutionOutcome = "ran" | "failed" | "not-executed"

/**
 * Narrow an `execution` envelope without re-deriving the rule at each call
 * site — the mistake being prevented is treating a missing `ok` as `false`.
 */
export function approvalExecutionOutcome(execution: ApprovalExecution | null | undefined): ApprovalExecutionOutcome {
  if (execution?.ok === true) return "ran"
  if (execution?.ok === false) return "failed"
  return "not-executed"
}

export interface Approval {
  id: string
  /** Null for approvals raised by a routine, which carry only a thread. */
  run_id: string | null
  bot_id: string
  risk: RiskClass
  title: string
  summary: string
  /**
   * The held action. Structurally this is an {@link ApprovalPayload}; it is
   * typed loosely here because the column is JSONB and older rows predate the
   * `kind` discriminator. Narrow it with `parseApprovalPayload()`.
   */
  payload: Record<string, unknown>
  status: ApprovalStatus
  created_at: string
  decided_by?: Uuid | null
  decided_at?: IsoDateTime | null
  /** Reason the human gave, if any. */
  note?: string | null
  /**
   * Result of replaying the payload. Present only on the response to
   * `POST /approvals/{id}/decide` with `approved`.
   */
  execution?: ApprovalExecution | null
}

/**
 * One standing permission — *"don't ask again for this button"*.
 *
 * `GET /standing-approvals`. A grant of authority for one person, one bot, one
 * control, on one page: the gate records why it did not stop instead of parking
 * the step for a decision.
 *
 * Two halves, and both are load-bearing. `permits`, `place` and `element` are
 * what the person reads, rendered server-side from the same vocabulary the chat
 * reply and the approval card use — a client that re-derived them would be a
 * third description of one grant. `origin`, `note`, `source_approval_ids` and
 * `granted_at` are the provenance: the answer to "who allowed this and on what
 * evidence", which is the question an audit opens with. Do not render one half
 * without the other.
 */
export interface StandingApproval {
  id: Uuid
  bot_id: Uuid
  /** `browser_click`, `browser_type`, `browser_select`. */
  action: string
  /** Always `send`. Money and destruction are never learned. */
  risk: RiskClass
  /** `button "Message"` — the accessible name Chrome computed, quoted. */
  element: string
  /** scheme+host+path, as matched. The host is half the key. */
  url: string
  /** `linkedin.com/in/andrei-pop` — the page as a person reads it. */
  place: string
  /** `click "Message" on linkedin.com/in/andrei-pop`. */
  permits: string
  /** `note` when they asked in writing, `repetition` when they kept saying yes. */
  origin: "note" | "repetition" | (string & {})
  /** Verbatim, when they asked in writing. Empty otherwise. */
  note: string
  /** The approvals this was learned from, oldest first. Never empty. */
  source_approval_ids: string[]
  used: number
  last_used_at?: IsoDateTime | null
  granted_at: IsoDateTime
  /** Set once revoked. The row is kept; the permission is not. */
  revoked_at?: IsoDateTime | null
}

/**
 * `GET /standing-approvals`.
 *
 * `always_asks` rides along with the collection rather than living in a client,
 * because the sentence is a promise about the server-side gate and a UI that
 * has to restate it is a UI that can restate it wrong.
 */
export interface StandingApprovalList {
  items: StandingApproval[]
  always_asks: string
}

/* ------------------------------------------------------------------ *
 * Bot Desktop
 * ------------------------------------------------------------------ */

/**
 * `POST /bots/{bot_id}/desktop/stream/ticket` — a short-lived capability to
 * watch one bot's desktop through the API's stream proxy.
 *
 * **This, not `BotDesktop.stream_url`, is how a client views a desktop.** A Bot
 * Desktop has no public address — one hypervisor-isolated container group per
 * bot on a delegated subnet, which is the isolation claim the product is built
 * on — so `stream_url` is a `10.60.x.x` address no client machine can route to.
 * Pointing a webview at it yields "This content is blocked". The API proxies the
 * stream instead, because it already sits inside the VNet.
 *
 * Neither an `<iframe src>` nor a WebSocket handshake can carry an
 * `Authorization` header, so both legs authenticate with this ticket: signed,
 * 60-second TTL, in the URL **path** so noVNC's relative asset fetches inherit
 * it. The session JWT is never exposed, and authorization is re-checked on
 * redeem, so a ticket cannot outlive the access that minted it.
 *
 * `stream_path` and `ws_path` are relative to the API root (the `/api` mount),
 * **not** to the host — the API does not guess its own public origin. Join them
 * to the origin of your base URL, not to the base URL itself, or you get
 * `/api/api/…`.
 */
export interface DesktopStreamTicket {
  ticket: string
  expires_at: IsoDateTime
  /** Whole seconds of validity left when the response was built. */
  expires_in: number
  /** noVNC's HTML/JS/CSS, e.g. `/bots/{id}/desktop/stream/{ticket}/vnc.html`. */
  stream_path: string
  /**
   * The VNC transport. **Single-use**: connecting burns the ticket, and a
   * second socket presenting it is closed with `4401`. Asset fetches under
   * `stream_path` keep working until it expires, because noVNC is still
   * loading files after it connects.
   */
  ws_path: string
  /**
   * What the desktop's VNC server expects, so the viewer does not have to stop
   * and ask a human for a password they were never told. Not the security
   * boundary — the private IP and the ticket are.
   */
  vnc_password?: string | null
}

export interface BotDesktop {
  bot_id: string
  state: BotDesktopState
  stream_url?: string | null
  control_url?: string | null
  container_id?: string | null
  last_error?: string | null
  updated_at?: IsoDateTime
}

/** `GET /bots/{bot_id}/desktop/screenshot`. */
export interface DesktopScreenshot {
  ok: boolean
  width: number
  height: number
  /** Raw base64 (no `data:` prefix). */
  png_base64: string
  /** True when this is the generated placeholder, not a real frame. */
  mock?: boolean
  error?: string | null
}

export interface DesktopWindow {
  /** X11 window ids arrive as numbers from some sidecar builds. */
  id: string | number
  title: string
  x?: number
  y?: number
  width?: number
  height?: number
  focused?: boolean
}

/**
 * `POST /bots/{bot_id}/desktop/action` when the action was allowed to run.
 *
 * A gated action answers 201 with {@link PendingApprovalResponse} instead, so
 * a caller must branch on the status code or on the presence of `approval_id`.
 * The body is the sidecar's own JSON in `docker`/`aks` mode, hence the index
 * signature.
 */
export interface DesktopActionResult {
  ok: boolean
  action?: string
  payload?: Record<string, unknown>
  result?: unknown
  error?: string
  /** True in `BOT_DESKTOP_MODE=mock` — nothing actually happened. */
  mock?: boolean
  [key: string]: unknown
}

/** `GET /bots/{bot_id}/desktop/windows`. */
export interface DesktopWindowList {
  ok: boolean
  windows: DesktopWindow[]
  mock?: boolean
  error?: string | null
}

/* ------------------------------------------------------------------ *
 * Connectors
 * ------------------------------------------------------------------ */

export interface ConnectorManifest {
  id: string
  name: string
  version: string
  auth: ConnectorAuthKind
  scopes: string[]
  actions: ConnectorAction[]
  risk_default: RiskClass
  first_party: boolean
}

export interface ConnectorAction {
  name: string
  description: string
  risk: RiskClass
  /** JSON Schema for the action input. */
  input_schema: Record<string, unknown>
}

/** A connector as bound to one bot. `GET /bots/{bot_id}/connectors`. */
export interface BotConnectorBinding {
  bot_id: Uuid
  connector_id: string
  /** Catalog display name, denormalised so the UI needs one request. */
  name: string
  status: ConnectorBindingStatus
  /** Key Vault reference, never the secret itself. */
  secret_ref?: string | null
  risk_default: RiskClass
  first_party: boolean
  actions: ConnectorAction[]
}

/**
 * A connector action that actually ran (HTTP 200).
 *
 * Shape comes from `execute_connector_action` in
 * `apps/api/app/services/connectors.py`. It always carries `ok`; everything
 * else depends on the connector and on how far the call got, hence the index
 * signature. When the action was gated the endpoint answers 201 with
 * {@link PendingApprovalResponse} instead — see {@link ConnectorActionOutcome}.
 */
export interface ConnectorActionResult {
  ok: boolean
  connector?: string
  action?: string
  input?: Record<string, unknown>
  /** The vendor payload on success. */
  result?: unknown
  error?: string
  /** Required inputs the call was missing, when `error` is a validation failure. */
  missing?: string[]
  /** True when no credential was resolvable and a mock payload was returned. */
  mock?: boolean
  /** True when a bound secret was resolved and used. */
  authenticated?: boolean
  /** The connector is not bound to this bot. */
  needs_auth?: boolean
  /** Set by the service layer when the risk gate blocked a direct call. */
  needs_approval?: boolean
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * MCP
 * ------------------------------------------------------------------ */

export interface McpServer {
  id: string
  name: string
  transport: McpTransport
  endpoint?: string
  command?: string
  enabled: boolean
  tool_allowlist: string[]
}

/** One entry of `GET /integrations/mcp/{id}/tools`. */
/**
 * A tool descriptor from an MCP server.
 *
 * `GET /integrations/mcp/{id}/tools` passes these through verbatim, so the
 * field names are MCP's, not ours: the MCP wire format spells the schema
 * `inputSchema` in camelCase and there is no snake_case variant to fall back
 * on. Do not add `input_schema` here — declaring a field the server never
 * populates is how a client ends up reading `undefined` forever.
 *
 * (The *connector manifest* in this package does use snake_case
 * `input_schema` — see {@link ConnectorAction}. Different schema, different
 * owner, deliberately different spelling.)
 */
export interface McpTool {
  name: string
  description?: string
  inputSchema?: Record<string, unknown>
}

export interface McpToolList {
  mcp_id: Uuid
  name: string
  tools: McpTool[]
  /** True when the server was unreachable and the list is a stand-in. */
  mock?: boolean
  error?: string | null
}

export interface McpCallResult {
  ok: boolean
  result?: unknown
  error?: string
}

/* ------------------------------------------------------------------ *
 * Routines
 * ------------------------------------------------------------------ */

export interface Routine {
  id: string
  bot_id: string
  /** Null for routines seeded with a system bot; set for user-created ones. */
  owner_user_id?: Uuid | null
  name: string
  description: string
  steps: RoutineStep[]
  schedule_cron?: string | null
  version: number
  enabled: boolean
  created_at?: IsoDateTime
}

export interface RoutineStep {
  type: "connector" | "mcp" | "desktop" | "approval"
  action: string
  args: Record<string, unknown>
}

/** `POST /routines/{id}/run`. */
export interface RoutineRunHandle {
  workflow_id: string | null
  run_id: string | null
  /** True when Temporal was unreachable and the steps ran inline instead. */
  inline: boolean
  status: string
  detail?: string | null
}

/* ------------------------------------------------------------------ *
 * Memory & knowledge base
 * ------------------------------------------------------------------ */

export type MemoryKind = "fact" | "preference" | "contact" | "procedure" | "note" | (string & {})

export interface Memory {
  id: Uuid
  bot_id?: Uuid | null
  user_id?: Uuid | null
  kind: MemoryKind
  content: string
  created_at: IsoDateTime
}

export interface KbArticle {
  id: Uuid
  title: string
  body: string
  created_at: IsoDateTime
}

/** `GET /kb?q=` — `score` is present only for vector search hits. */
export interface KbSearchResult extends KbArticle {
  score?: number | null
}

/* ------------------------------------------------------------------ *
 * Cost & usage
 * ------------------------------------------------------------------ */

export interface CostLedgerEntry {
  id: string
  bot_id: string
  tier: ModelTier
  input_tokens: number
  output_tokens: number
  cost_usd: number
  created_at: string
}

/**
 * One entry inside a {@link UsageSummary}.
 *
 * Deliberately NOT a {@link CostLedgerEntry}: the usage router projects the
 * ledger by hand (`apps/api/app/routers/usage.py`) and omits `id` and
 * `bot_id` — the bot is already on the enclosing row.
 */
export interface UsageEntry {
  tier: ModelTier
  input_tokens: number
  output_tokens: number
  cost_usd: number
  created_at: IsoDateTime
}

/** One row of `GET /usage?days=`. Capped at the 50 most recent entries. */
export interface UsageSummary {
  bot_id: Uuid
  bot_name: string
  spent_usd_today: number
  budget_usd: number
  entries: UsageEntry[]
}

/* ------------------------------------------------------------------ *
 * Evals
 * ------------------------------------------------------------------ */

export interface EvalCase {
  name: string
  prompt: string
  expect_contains: string[]
}

export interface EvalResult {
  name: string
  passed: boolean
  output: string
  /** Expectations that were not found in `output`. */
  missing: string[]
  /** Evals run on the `mini` tier — never the flagship. */
  tier?: ModelTier
  cost_usd?: number
}

/** `POST /evals/suite`. */
export interface EvalSuiteResult {
  passed: number
  total: number
  results: EvalResult[]
  /** Total spend for the suite. Evals cost money; show it. */
  cost_usd: number
}

/* ------------------------------------------------------------------ *
 * Health
 * ------------------------------------------------------------------ */

/** `GET /health`. */
export interface Health {
  ok: boolean
  service: string
  /** Hand-maintained API contract version. Changes when the contract changes. */
  version: string
  /** Image tag stamped at build time, or "unknown" when unstamped. */
  build: string
}

export interface HealthCheck {
  ok: boolean
  detail?: string | null
  latency_ms?: number | null
}

/**
 * A dependency check. The API may answer with either a bare boolean or a
 * detail object depending on the dependency — use `isHealthy()` rather than
 * reading `.ok` directly.
 */
export type HealthCheckResult = boolean | HealthCheck

export interface DeepHealthChecks {
  db: HealthCheckResult
  redis: HealthCheckResult
  temporal: HealthCheckResult
  /** The API may report additional dependencies without a client release. */
  [dependency: string]: HealthCheckResult
}

/** `GET /health/deep`. 503 when `checks.db` is down. */
export interface DeepHealth {
  ok: boolean
  /** Always present — `service` is defaulted and `version` is required. */
  service: string
  version: string
  checks: DeepHealthChecks
}

export function isHealthy(check: HealthCheckResult | undefined): boolean {
  if (typeof check === "boolean") return check
  return check?.ok === true
}

/* ------------------------------------------------------------------ *
 * Devices (push targets for the mobile app)
 * ------------------------------------------------------------------ */

export type DevicePlatform = "ios" | "android" | "web"

export const DEVICE_PLATFORMS = ["ios", "android", "web"] as const satisfies readonly DevicePlatform[]

/**
 * A registered push target. Approvals are why this exists: an agent that
 * blocks on a human needs a way to reach that human.
 *
 * Registered with `POST /me/devices`, removed with `DELETE /me/devices/{token}`.
 * Unique per `(user_id, token)`; re-registering the same token updates it.
 */
export interface Device {
  id: Uuid
  user_id: Uuid
  /** Expo / APNs / FCM token. Treated as a secret; never log it. */
  token: string
  platform: DevicePlatform
  created_at?: IsoDateTime | null
}

/* ------------------------------------------------------------------ *
 * Work items — owned, transferable work, and the ledger of who held it
 * ------------------------------------------------------------------ */

/**
 * An external identity a work item is recognised by when something arrives
 * from outside — the address on a reply, the number on an SMS, the record id
 * on a CRM webhook.
 *
 * Normalised server-side (trimmed, lowercased) on both write and lookup, so a
 * client should not try to canonicalise before sending. Deliberately not
 * unique across items: the same person can honestly be two work items, and the
 * API resolves that with ordered candidates rather than an error.
 */
export interface WorkItemKey {
  /** The space the value lives in: `email`, `phone`, `linkedin`, `crm`, … */
  channel: string
  value: string
}

/**
 * A unit of owned, transferable work — the object one bot hands to another.
 *
 * Generalised with a `type` rather than modelled per use case. A lead is the
 * motivating example, not the mechanism; a support escalation and an invoice
 * exception need the same owning bot, the same owning human, and the same
 * recorded history of who was holding it when.
 *
 * `owner_user_id` never changes. `owner_bot_id` is what moves, and only through
 * `POST /work-items/{id}/transfer` — `PATCH` refuses the field with a 422
 * rather than ignoring it, because that path is the only one that writes the
 * ledger.
 */
export interface WorkItem {
  id: Uuid
  /** Free text (`lead`, `ticket`, `invoice`, …); adding one needs no migration. */
  type: string
  title: string
  summary: string
  status: WorkItemStatus
  /** The outcome, once `status` is terminal. Null while it is still live. */
  resolution?: string | null
  /** Null only when the owning bot was deleted out from under the item. */
  owner_bot_id?: Uuid | null
  owner_user_id: Uuid
  /** The conversation the bots are having about this, when there is one. */
  thread_id?: Uuid | null
  detail: Record<string, unknown>
  keys: WorkItemKey[]
  created_at: IsoDateTime
  updated_at: IsoDateTime
  /** Null until it has actually been handed over at least once. */
  transferred_at?: IsoDateTime | null
  /**
   * Last time the **outside world** touched this — a reply landing, not an
   * edit. Distinct from `updated_at`, which any change moves; this is the one
   * that answers "has the lead answered yet?".
   */
  last_event_at?: IsoDateTime | null
  closed_at?: IsoDateTime | null
}

/**
 * One row of the handover ledger: who gave what to whom, when, and why.
 *
 * Stored without foreign keys, like audit events, so deleting the work item
 * does not erase the record that it was handed over. Exactly one row per item
 * has `from_bot_id: null` — the opening assignment written at creation, which
 * is what makes "read the transfers" a complete answer rather than one with the
 * first holder missing.
 */
export interface WorkItemTransfer {
  id: Uuid
  work_item_id: Uuid
  owner_user_id: Uuid
  /** Null on the opening row: there was no predecessor. */
  from_bot_id?: Uuid | null
  to_bot_id: Uuid
  /** The human behind the handover, when there was one. */
  actor_user_id?: Uuid | null
  /** The bot that initiated it, when a bot did. */
  actor_bot_id?: Uuid | null
  /** Required by the API. A ledger of timestamps with no reasons is not one. */
  reason: string
  /** How it was triggered: `create`, `api`, or the delegation lane's own value. */
  source: string
  detail: Record<string, unknown>
  created_at: IsoDateTime
}

/**
 * What `POST /work-items/{id}/transfer` answers.
 *
 * `transferred` is the idempotency answer, the same shape and the same
 * reasoning as `ResumeRunResponse.resumed`: handing an item to the bot that
 * already holds it is a retry, not an error, so it comes back `ok: true,
 * transferred: false` with the item unchanged and **no** second ledger row
 * asserting a handover that did not happen.
 */
export interface WorkItemTransferResult {
  ok: boolean
  transferred: boolean
  work_item: WorkItem
  /** Absent when `transferred` is false — nothing was recorded. */
  transfer?: WorkItemTransfer | null
  detail?: string | null
}

/* ------------------------------------------------------------------ *
 * Inbound events
 * ------------------------------------------------------------------ */

/**
 * A configured way for the outside world to reach a bot.
 *
 * `slug` is server-generated and unguessable — it is the last segment of
 * `hook_path`, and the column is globally unique, so it is neither chosen nor
 * editable by a client. Treat it as a capability URL: the HMAC over the signing
 * key named by `secret_ref` is what actually authenticates a delivery.
 *
 * `secret_ref` is a **reference** (`env://NAME`, `kv://vault/name`), never the
 * key. The API validates the shape on write, so this field cannot come to hold
 * a plaintext secret.
 */
export interface InboundSource {
  id: Uuid
  /** The public path segment. Server-generated; not editable. */
  slug: string
  /** `POST` here to deliver — the full path, ready to paste into a provider. */
  hook_path: string
  name: string
  kind: InboundSourceKind
  /** Default `WorkItemKey.channel` for deliveries that do not name one. */
  channel: string
  owner_user_id: Uuid
  /** The bot a poll runs as. Null on a webhook source that names none. */
  bot_id?: Uuid | null
  /**
   * Bots seated when this lane has to **create** a thread for a reply.
   *
   * This is how a reply can reach Sales at all: thread membership is the
   * delegation boundary, so the roster has to be named by a human ahead of
   * time. Nothing a model says can add to it.
   */
  bot_ids: Uuid[]
  /** A reference to the signing key, never the key. */
  secret_ref?: string | null
  connector_id?: string | null
  config: Record<string, unknown>
  /** A disabled source refuses deliveries exactly as an unknown slug does. */
  enabled: boolean
  last_event_at?: IsoDateTime | null
  last_polled_at?: IsoDateTime | null
  created_at: IsoDateTime
  updated_at: IsoDateTime
}

/**
 * One thing that arrived from outside, matched or not.
 *
 * `subject` and `body` are what the sender actually sent, stored untouched.
 * **Render them as text.** They are the only attacker-controlled strings in this
 * API; the sanitising the server does happens on the way to the model, not on
 * the way to a client.
 */
export interface InboundEvent {
  id: Uuid
  /** Null once the source it came through has been deleted. */
  source_id?: Uuid | null
  owner_user_id: Uuid
  channel: string
  /** The normalised key this resolved against. A `From` header is forgeable. */
  address: string
  external_id: string
  /** `webhook` or `poll`. */
  via: string
  status: InboundEventStatus
  subject: string
  body: string
  work_item_id?: Uuid | null
  /**
   * Every candidate, in resolution order, when the address matched more than
   * one. `work_item_id` is always the first; the rest are here so a wrong guess
   * is visible instead of silent.
   */
  candidate_ids: Uuid[]
  thread_id?: Uuid | null
  run_id?: Uuid | null
  detail: Record<string, unknown>
  created_at: IsoDateTime
  /** Null until the owning bot has actually been woken. */
  handled_at?: IsoDateTime | null
}

/**
 * The **constant** answer to every authenticated delivery.
 *
 * Byte-identical whether the reply matched a live lead, matched five, matched
 * none, or was a replay. That is deliberate: the endpoint is unauthenticated, so
 * anything that varied with the tenant's data would be a way to probe it. The
 * owner reads the real outcome off `InboundEvent`.
 */
export interface InboundAck {
  ok: boolean
  status: string
}

/** What one poll fetched, and what became of each record. */
export interface InboundPollResult {
  ok: boolean
  source_id: Uuid
  fetched: number
  matched: number
  ambiguous: number
  unmatched: number
  unroutable: number
  duplicates: number
  event_ids: Uuid[]
  detail?: string | null
}
