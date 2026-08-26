/**
 * Core enumerations shared by every Nesq Bot surface.
 *
 * These mirror the string literals the Python API stores in Postgres text
 * columns (see `apps/api/app/models.py`). Keep them in sync with
 * `docs/API.md` — that file is the contract, this one is its TypeScript face.
 */

/* ------------------------------------------------------------------ *
 * Risk
 * ------------------------------------------------------------------ */

export type RiskClass = "observe" | "draft" | "mutate" | "send" | "spend" | "delete"

/** Every risk class, ordered from least to most dangerous. */
export const RISK_CLASSES = [
  "observe",
  "draft",
  "mutate",
  "send",
  "spend",
  "delete",
] as const satisfies readonly RiskClass[]

/** Sort weight for a risk class — higher is more dangerous. */
export const RISK_ORDER: Record<RiskClass, number> = {
  observe: 0,
  draft: 1,
  mutate: 2,
  send: 3,
  spend: 4,
  delete: 5,
}

/**
 * Risk classes that can never execute without a human decision.
 *
 * The API enforces this server-side; clients use it to render the right
 * affordance ("Send" vs "Request approval") before the round trip.
 */
export const APPROVAL_REQUIRED_RISKS = ["send", "spend", "delete"] as const satisfies readonly RiskClass[]

export function isRiskClass(value: unknown): value is RiskClass {
  return typeof value === "string" && (RISK_CLASSES as readonly string[]).includes(value)
}

/** Mirrors `requires_approval(risk)` in the API. */
export function requiresApproval(risk: RiskClass): boolean {
  return (APPROVAL_REQUIRED_RISKS as readonly string[]).includes(risk)
}

/** True when `risk` is at least as dangerous as `floor`. */
export function isAtLeastRisk(risk: RiskClass, floor: RiskClass): boolean {
  return RISK_ORDER[risk] >= RISK_ORDER[floor]
}

/** Comparator for sorting by danger, ascending. */
export function compareRisk(a: RiskClass, b: RiskClass): number {
  return RISK_ORDER[a] - RISK_ORDER[b]
}

/**
 * Keyword table that classifies a Bot Desktop action name into a risk class.
 *
 * Mirrors `_RISK_KEYWORDS` in `apps/api/app/routers/desktop.py`, which is the
 * source of truth. Ordered most-dangerous first: the first group that matches
 * as a substring of the lower-cased action name wins.
 */
export const DESKTOP_ACTION_RISK_KEYWORDS: readonly (readonly [readonly string[], RiskClass])[] = [
  [["delete", "remove", "erase", "wipe", "drop", "trash"], "delete"],
  [["pay", "purchase", "buy", "checkout", "order", "spend", "transfer"], "spend"],
  [["send", "submit", "post", "publish", "reply", "email", "share"], "send"],
]

/**
 * Classify a desktop action name, mirroring `desktop_action_risk()` in the API.
 *
 * There is no manifest for computer use — a bot clicking a Send button is
 * indistinguishable from a bot clicking anything else — so the action *name*
 * is what gets classified. Clients use this to label a control before it is
 * pressed ("this will need approval"); the API classifies again server-side,
 * and the server's answer is the one that decides.
 */
export function desktopActionRisk(action: string): RiskClass {
  const lowered = (action || "").toLowerCase()
  for (const [keywords, risk] of DESKTOP_ACTION_RISK_KEYWORDS) {
    if (keywords.some((keyword) => lowered.includes(keyword))) return risk
  }
  return "observe"
}

/* ------------------------------------------------------------------ *
 * Models
 * ------------------------------------------------------------------ */

export type ModelTier = "nano" | "mini" | "reason" | "embed"

export const MODEL_TIERS = ["nano", "mini", "reason", "embed"] as const satisfies readonly ModelTier[]

export function isModelTier(value: unknown): value is ModelTier {
  return typeof value === "string" && (MODEL_TIERS as readonly string[]).includes(value)
}

/**
 * Mirrors `Provider` in `apps/api/app/services/model_router.py` and
 * `KNOWN_MODEL_PROVIDERS` in `apps/api/app/schemas.py`. `anthropic`/`google`
 * are valid values a bot can be pinned to — the API accepts them — but have
 * no live client yet; `GET /bots/providers` is how a caller learns which of
 * the four are actually reachable in a given deployment.
 */
export type ModelProvider = "azure" | "openai" | "anthropic" | "google"

export const MODEL_PROVIDERS = ["azure", "openai", "anthropic", "google"] as const satisfies readonly ModelProvider[]

export function isModelProvider(value: unknown): value is ModelProvider {
  return typeof value === "string" && (MODEL_PROVIDERS as readonly string[]).includes(value)
}

/* ------------------------------------------------------------------ *
 * Runs
 * ------------------------------------------------------------------ */

/**
 * The states a run can be in.
 *
 * Two of them are *parked* rather than finished, and the difference is who the
 * run is waiting on:
 *
 * - `awaiting_approval` — waiting for a yes or a no on one held action.
 *   `POST /approvals/{id}/decide` answers it, and the decision continues the
 *   run through the same machinery the resume button uses.
 * - `awaiting_human` — waiting for a person at the screen: a login, an MFA
 *   prompt, a CAPTCHA. `POST /runs/{run_id}/resume` answers it.
 *
 * `awaiting_human` was missing here after the API shipped it, so
 * `run.status === "awaiting_human"` did not typecheck and both clients had to
 * widen the type locally to name the state this product is built around.
 */
export type RunStatus =
  "queued" | "running" | "awaiting_approval" | "awaiting_human" | "completed" | "failed" | "cancelled"

export const RUN_STATUSES = [
  "queued",
  "running",
  "awaiting_approval",
  "awaiting_human",
  "completed",
  "failed",
  "cancelled",
] as const satisfies readonly RunStatus[]

/** A run in one of these states will never change again. */
export const TERMINAL_RUN_STATUSES = ["completed", "failed", "cancelled"] as const satisfies readonly RunStatus[]

/**
 * Stopped, and waiting on a person. Not terminal — a parked run moves again
 * the moment someone decides or presses Continue, which is the whole premise
 * of the approvals and takeover surfaces.
 */
export const PARKED_RUN_STATUSES = ["awaiting_approval", "awaiting_human"] as const satisfies readonly RunStatus[]

export function isRunStatus(value: unknown): value is RunStatus {
  return typeof value === "string" && (RUN_STATUSES as readonly string[]).includes(value)
}

/** Stop polling / close the stream once this returns true. */
export function isTerminalRunStatus(status: RunStatus): boolean {
  return (TERMINAL_RUN_STATUSES as readonly string[]).includes(status)
}

/**
 * True when the run has stopped and only a human will move it.
 *
 * Takes a plain string as well as a `RunStatus`: `RunOut.status` is `str` on
 * the wire, and a client should not have to assert before asking this.
 */
export function isParkedRunStatus(status: RunStatus | (string & {})): boolean {
  return (PARKED_RUN_STATUSES as readonly string[]).includes(status)
}

/* ------------------------------------------------------------------ *
 * Approvals
 * ------------------------------------------------------------------ */

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired"

export const APPROVAL_STATUSES = [
  "pending",
  "approved",
  "rejected",
  "expired",
] as const satisfies readonly ApprovalStatus[]

export function isApprovalStatus(value: unknown): value is ApprovalStatus {
  return typeof value === "string" && (APPROVAL_STATUSES as readonly string[]).includes(value)
}

/** Only pending approvals can be decided; anything else answers 409. */
export function isDecidableApprovalStatus(status: ApprovalStatus): boolean {
  return status === "pending"
}

/* ------------------------------------------------------------------ *
 * Bot Desktop
 * ------------------------------------------------------------------ */

export type BotDesktopState = "absent" | "starting" | "running" | "suspended" | "stopping" | "error"

export const BOT_DESKTOP_STATES = [
  "absent",
  "starting",
  "running",
  "suspended",
  "stopping",
  "error",
] as const satisfies readonly BotDesktopState[]

export function isBotDesktopState(value: unknown): value is BotDesktopState {
  return typeof value === "string" && (BOT_DESKTOP_STATES as readonly string[]).includes(value)
}

/** A desktop that can accept `/desktop/action` right now. */
export function isDesktopInteractive(state: BotDesktopState): boolean {
  return state === "running"
}

/* ------------------------------------------------------------------ *
 * Misc shared literals
 * ------------------------------------------------------------------ */

export type DesktopProfile = "xfce" | "icewm"

export const DESKTOP_PROFILES = ["xfce", "icewm"] as const satisfies readonly DesktopProfile[]

export type MessageRole = "user" | "assistant" | "system" | "tool"

export type ConnectorAuthKind = "oauth2" | "api_key" | "none"

export type ConnectorBindingStatus = "connected" | "disconnected" | "error"

export type McpTransport = "stdio" | "sse" | "http"

/** ISO-8601 timestamp string as emitted by FastAPI/pydantic. */
export type IsoDateTime = string

/** UUID string as emitted by FastAPI/pydantic. */
export type Uuid = string

/* ------------------------------------------------------------------ *
 * Work items
 * ------------------------------------------------------------------ */

/**
 * The four states a work item can be in.
 *
 * Deliberately narrow. `open` is "nobody has touched it", `working` is "the
 * owning bot is acting", `waiting` is "blocked on the outside world" — where a
 * lead sits between an outreach going out and a reply coming back — and
 * `closed` is terminal, with the outcome in `WorkItem.resolution`. Anything
 * finer belongs in `resolution` or `detail`, not in a state machine every
 * client has to reason about.
 */
export type WorkItemStatus = "open" | "working" | "waiting" | "closed"

export const WORK_ITEM_STATUSES = ["open", "working", "waiting", "closed"] as const satisfies readonly WorkItemStatus[]

export function isWorkItemStatus(value: unknown): value is WorkItemStatus {
  return typeof value === "string" && (WORK_ITEM_STATUSES as readonly string[]).includes(value)
}

/** True when nothing more is expected of the item. Only `closed` qualifies. */
export function isWorkItemClosed(status: WorkItemStatus): boolean {
  return status === "closed"
}

/* ------------------------------------------------------------------ *
 * Inbound events
 * ------------------------------------------------------------------ */

/**
 * How an inbound source is fed.
 *
 * `webhook` is pushed to over an unauthenticated, HMAC-signed URL; `poll` is
 * pulled from a connector the owner already bound. Both converge on one server
 * path before any decision is made about the message, which is what stops a
 * reply delivered by email being handled differently from the same reply pulled
 * out of a mailbox.
 */
export type InboundSourceKind = "webhook" | "poll"

export const INBOUND_SOURCE_KINDS = ["webhook", "poll"] as const satisfies readonly InboundSourceKind[]

/**
 * What became of one delivery. **None of these means "discarded".**
 *
 * - `matched`    one work item; its owning bot was woken.
 * - `ambiguous`  several candidates; the first was taken, the rest recorded.
 * - `unmatched`  no work item. A queue a person works, not an error.
 * - `unroutable` an item, but no bot or no human answerable for it.
 * - `duplicate`  a replay of a delivery already on the record; no new row.
 */
export type InboundEventStatus = "matched" | "ambiguous" | "unmatched" | "unroutable" | "duplicate"

export const INBOUND_EVENT_STATUSES = [
  "matched",
  "ambiguous",
  "unmatched",
  "unroutable",
  "duplicate",
] as const satisfies readonly InboundEventStatus[]

export function isInboundEventStatus(value: unknown): value is InboundEventStatus {
  return typeof value === "string" && (INBOUND_EVENT_STATUSES as readonly string[]).includes(value)
}

/** True when a person still has to place this reply by hand. */
export function inboundNeedsAttention(status: InboundEventStatus): boolean {
  return status === "unmatched" || status === "unroutable"
}
