/**
 * Request and response bodies, one per endpoint in `docs/API.md`.
 *
 * Naming: `XxxRequest` / `XxxResponse`. The `*In` / `*Out` aliases that mirror
 * the pydantic class names live in `index.ts`.
 */

import type {
  ApprovalStatus,
  ConnectorAuthKind,
  ConnectorBindingStatus,
  DesktopProfile,
  McpTransport,
  ModelProvider,
  ModelTier,
  RiskClass,
  RunStatus,
  Uuid,
  InboundSourceKind,
  WorkItemStatus,
} from "./core"
import type { DesktopActionName } from "./approvals"
import type {
  Approval,
  ConnectorAction,
  ConnectorActionResult,
  DesktopActionResult,
  RoutineStep,
  DevicePlatform,
  EvalCase,
  EvalResult,
  MemoryKind,
  User,
  WorkItemKey,
} from "./entities"

/* ------------------------------------------------------------------ *
 * Auth
 * ------------------------------------------------------------------ */

/** `POST /auth/dev-login` and `POST /auth/entra`. */
export interface TokenResponse {
  access_token: string
  user: User
  token_type?: "bearer"
}

/** `POST /auth/entra` — the id_token from MSAL. */
export interface EntraLoginRequest {
  id_token: string
}

/* ------------------------------------------------------------------ *
 * Bots
 * ------------------------------------------------------------------ */

export interface CreateCustomBotRequest {
  name: string
  role: string
  system_prompt: string
  connector_ids?: string[]
  mcp_ids?: string[]
  desktop_profile?: "xfce" | "icewm"
  daily_budget_usd?: number
  /** Both or neither — 422 on one without the other. See `Bot.model_provider`. */
  model_provider?: ModelProvider | null
  model_name?: string | null
}

/**
 * `PATCH /bots/{bot_id}`. Every field optional; only what you send changes.
 * Sending `system_prompt` or a slug change for a system bot answers 403.
 *
 * `model_provider`/`model_name` are the one pair here where `null` is a
 * meaningful, distinct value from "not sent": send `null` on both to clear
 * an override and revert to tier routing. Sending only one, in a request
 * that leaves the *other* field inconsistent with what is already stored, is
 * a 422 (`incomplete_model_override`) — but sending only `model_name` to
 * swap the model under an already-configured provider is fine.
 */
export interface UpdateBotRequest {
  name?: string
  role?: string
  /** Custom bots only — 403 on a system bot. */
  slug?: string
  system_prompt?: string
  daily_budget_usd?: number
  desktop_profile?: DesktopProfile
  model_provider?: ModelProvider | null
  model_name?: string | null
}

/** `PATCH /bots/{bot_id}/budget`. */
export interface UpdateBudgetRequest {
  daily_budget_usd: number
}

/** `GET /bots/providers`. A live credential resolved, not just an accepted config value. */
export interface ProvidersResponse {
  azure: boolean
  openai: boolean
  anthropic: boolean
  google: boolean
}

/* ------------------------------------------------------------------ *
 * Threads & messages
 * ------------------------------------------------------------------ */

export interface CreateThreadRequest {
  bot_ids: string[]
  title?: string
  initial_message?: string
}

export interface SendMessageRequest {
  content: string
  mention_bot_ids?: string[]
}

/**
 * `POST /threads/{thread_id}/messages` — the non-streaming turn.
 * `approval_id` is non-null when the turn parked on a human decision.
 */
export interface SendMessageResponse {
  bot_id: Uuid
  message: string
  run_id: Uuid
  tier: ModelTier
  cost_usd: number
  approval_id: Uuid | null
  /** True when the bot refused to spend past its daily budget. */
  budget_blocked?: boolean
}

/* ------------------------------------------------------------------ *
 * Runs & audit
 * ------------------------------------------------------------------ */

export interface ListRunsQuery {
  thread_id?: Uuid
  bot_id?: Uuid
  status?: RunStatus
  limit?: number
}

export interface ListAuditQuery {
  bot_id?: Uuid
  event_type?: string
  limit?: number
  /** ISO timestamp — return events strictly older than this. */
  before?: string
}

/* ------------------------------------------------------------------ *
 * Approvals
 * ------------------------------------------------------------------ */

export interface ListApprovalsQuery {
  status?: ApprovalStatus
  bot_id?: Uuid
}

export type ApprovalDecision = "approved" | "rejected"

export interface ApprovalDecisionRequest {
  decision: ApprovalDecision
  note?: string
}

/**
 * `POST /approvals/{id}/decide`. The approval is returned in its new state;
 * `execution` is present only for `approved`. Deciding a non-pending
 * approval answers 409 `already_decided`.
 */
export type ApprovalDecisionResponse = Approval

/** Worker-facing: `POST /approvals` raises an approval outside a chat turn. */
export interface CreateApprovalRequest {
  bot_id: Uuid
  run_id?: Uuid | null
  risk?: RiskClass
  title: string
  summary?: string
  payload?: Record<string, unknown>
}

/* ------------------------------------------------------------------ *
 * Connectors
 * ------------------------------------------------------------------ */

/** `POST /integrations/connectors`. Mirrors `RegisterConnectorIn`. */
export interface RegisterConnectorRequest {
  id: string
  name: string
  version?: string
  auth?: ConnectorAuthKind
  scopes?: string[]
  actions?: ConnectorAction[]
  risk_default?: RiskClass
  first_party?: boolean
}

/** `POST /bots/{bot_id}/connectors/{connector_id}`. */
export interface BindConnectorRequest {
  /** Key Vault secret reference. The raw secret never crosses this boundary. */
  secret_ref?: string | null
  status?: ConnectorBindingStatus
}

/** `POST /bots/{bot_id}/connectors/{connector_id}/actions/{action}`. */
export interface ExecuteConnectorActionRequest {
  input?: Record<string, unknown>
  /** Overrides the approval card title when the action is gated. */
  title?: string
  /** Links the resulting approval back to a thread. */
  thread_id?: Uuid | null
}

/**
 * **201** answer from a risk-gated route when the action was held instead of
 * executed. Returned by both
 * `POST /bots/{id}/connectors/{cid}/actions/{action}` and
 * `POST /bots/{id}/desktop/action`.
 *
 * `approval_id` is typed as required and non-null on purpose. The pydantic
 * model declares it `UUID | None` defensively, but both call sites
 * (`routers/integrations.py`, `routers/desktop.py`) construct it from a freshly
 * persisted `approval.id`, so it is always present — and it has to be, because
 * it is the discriminant the outcome unions below branch on.
 */
export interface PendingApprovalResponse {
  approval_id: Uuid
  status: "pending_approval"
  risk: RiskClass
  title: string
  detail?: string | null
}

/* ------------------------------------------------------------------ *
 * Risk-gated action outcomes
 *
 * A gated route answers with one of two different shapes depending on the
 * action's risk class, and the status code is the only other signal:
 *
 *   200 -> it ran; the body is the executed result
 *   201 -> it was held; the body is a PendingApprovalResponse
 *
 * Both lanes independently built this union and a narrowing helper, which is
 * the signal it belongs here. "Outcome" rather than "Result" because half the
 * union is the case where nothing happened yet.
 * ------------------------------------------------------------------ */

/** `POST /bots/{id}/connectors/{cid}/actions/{action}` — ran, or was held. */
export type ConnectorActionOutcome = ConnectorActionResult | PendingApprovalResponse

/** `POST /bots/{id}/desktop/action` — ran, or was held. */
export type DesktopActionOutcome = DesktopActionResult | PendingApprovalResponse

/** Either gated route, for code that treats them the same. */
export type ActionOutcome = ConnectorActionOutcome | DesktopActionOutcome

/**
 * Narrow a gated response to the held branch.
 *
 * Branches on `approval_id` rather than on the status code, so it works on a
 * body that has already been separated from its response, and rather than on
 * `ok` — which the held shape does not have at all.
 */
export function isPendingApproval(value: unknown): value is PendingApprovalResponse {
  if (typeof value !== "object" || value === null) return false
  const candidate = value as { approval_id?: unknown }
  return typeof candidate.approval_id === "string" && candidate.approval_id.length > 0
}

/**
 * The held branch as a fully populated {@link PendingApprovalResponse}, or
 * `null` when the action ran.
 *
 * Fills in `risk` and `title` when a caller has a partial body, so an
 * approval banner always has something to render. Use {@link isPendingApproval}
 * when you only need the branch.
 */
export function asPendingApproval(value: unknown): PendingApprovalResponse | null {
  if (!isPendingApproval(value)) return null
  const candidate = value as Partial<PendingApprovalResponse>
  return {
    approval_id: candidate.approval_id as Uuid,
    status: "pending_approval",
    risk: (candidate.risk ?? "send") as RiskClass,
    title: typeof candidate.title === "string" ? candidate.title : "Action held for approval",
    detail: typeof candidate.detail === "string" ? candidate.detail : null,
  }
}

/* ------------------------------------------------------------------ *
 * MCP
 * ------------------------------------------------------------------ */

export interface RegisterMcpRequest {
  name: string
  transport: McpTransport
  endpoint?: string | null
  command?: string | null
  tool_allowlist?: string[]
}

/** `PATCH /integrations/mcp/{id}`. */
export interface UpdateMcpRequest {
  name?: string
  transport?: McpTransport
  endpoint?: string | null
  command?: string | null
  enabled?: boolean
  tool_allowlist?: string[]
}

/** `POST /bots/{bot_id}/mcp/{mcp_id}/call`. */
export interface CallMcpToolRequest {
  tool: string
  arguments?: Record<string, unknown>
}

/* ------------------------------------------------------------------ *
 * Bot Desktop
 * ------------------------------------------------------------------ */

/** `POST /bots/{bot_id}/desktop/action`. */
export interface DesktopActionRequest {
  action: DesktopActionName
  x?: number | null
  y?: number | null
  text?: string | null
  button?: string | null
  keys?: string[]
}

export interface StopDesktopQuery {
  /** Delete the bot's home volume as well. Destructive; `delete`-class. */
  wipe?: boolean
}

/* ------------------------------------------------------------------ *
 * Routines
 * ------------------------------------------------------------------ */

export interface CreateRoutineRequest {
  bot_id: Uuid
  name: string
  description?: string
  steps: Record<string, unknown>[]
  schedule_cron?: string | null
}

/**
 * One step captured by the desktop recorder, before it is promoted to a
 * {@link RoutineStep}.
 *
 * `POST /routines/teach` normalises each entry as
 * `{type: type ?? "desktop", action: action ?? "click", args: <everything else>}`
 * (`apps/api/app/routers/routines.py`). So every other key here — including
 * client-side bookkeeping like `uid` and `at` — is persisted into the step's
 * `args`. Strip what you do not want stored before sending.
 */
export interface RecordedStep {
  action: DesktopActionName
  /** Defaults to `"desktop"` server-side. */
  type?: RoutineStep["type"]
  x?: number
  y?: number
  text?: string
  button?: string
  keys?: string[]
  /** Human label for the recorder UI. Lands in `args` if you send it. */
  label?: string
  /** Client-side identity for list keys. Lands in `args` if you send it. */
  uid?: string
  /** Capture timestamp (epoch ms). Lands in `args` if you send it. */
  at?: number
  [key: string]: unknown
}

/** `POST /routines/teach` — promote a recorded desktop session to a routine. */
export interface TeachRoutineRequest {
  bot_id: Uuid
  name: string
  description?: string
  recorded_steps: RecordedStep[]
  schedule_cron?: string | null
}

/** `PATCH /routines/{id}`. Changing `steps` bumps `version`. */
export interface UpdateRoutineRequest {
  name?: string
  description?: string
  steps?: Record<string, unknown>[]
  schedule_cron?: string | null
  enabled?: boolean
}

/* ------------------------------------------------------------------ *
 * Memory & KB
 * ------------------------------------------------------------------ */

export interface CreateMemoryRequest {
  /** Defaults to `"note"`. */
  kind?: MemoryKind
  content: string
}

export interface CreateKbArticleRequest {
  title: string
  body: string
}

export interface UpdateKbArticleRequest {
  title?: string
  body?: string
}

export interface KbSearchQuery {
  q?: string
  limit?: number
}

/* ------------------------------------------------------------------ *
 * Usage & evals
 * ------------------------------------------------------------------ */

export interface UsageQuery {
  days?: number
}

/** `POST /evals/run`. */
export type RunEvalRequest = EvalCase

/** `POST /evals/suite`. */
export interface RunEvalSuiteRequest {
  cases: EvalCase[]
}

export interface RunEvalSuiteResponse {
  passed: number
  total: number
  results: EvalResult[]
  cost_usd: number
}

/** Worker-facing: `POST /runs/{run_id}/status`, how a workflow reports progress. */
export interface UpdateRunStatusRequest {
  status: RunStatus
  error?: string | null
  detail?: Record<string, unknown> | null
  routine_id?: Uuid | null
  thread_id?: Uuid | null
  bot_id?: Uuid | null
  workflow_id?: string | null
}

/* ------------------------------------------------------------------ *
 * Human takeover — the resume button
 * ------------------------------------------------------------------ */

/**
 * Body of `POST /runs/{run_id}/resume`.
 *
 * Everything the resumed run needs is already persisted on the run; `note` is
 * the one thing only the person at the screen knows ("logged in as norbert@…").
 * It goes into the transcript the model is rebuilt from, so it must never carry
 * a credential.
 */
export interface ResumeRunRequest {
  /** Max 2000 characters server-side. */
  note?: string
}

/**
 * Answer to `POST /runs/{run_id}/resume`.
 *
 * `resumed: false` is **not an error**. The endpoint claims the run with a
 * single conditional `awaiting_human -> running` update, so a double-press
 * loses the race and is told so rather than starting a second agent loop on the
 * same browser session. Render it as "already going", never as a failure — a
 * client that shows an error there teaches people to press the button twice,
 * which is exactly what the idempotency is defending against.
 *
 * The remaining fields describe the resumed turn when this call is the one that
 * ran it: a run resumed synchronously answers with the reply it produced.
 */
export interface ResumeRunResponse {
  ok: boolean
  resumed: boolean
  run_id: Uuid
  /** The run's status after the call. */
  status: string
  /** Why it was not resumed, when it was not. */
  detail?: string | null
  thread_id?: Uuid | null
  bot_id?: Uuid | null
  message_id?: Uuid | null
  /** The reply text the resumed run produced. */
  message?: string | null
  /** How the resumed agent loop ended — including parking again. */
  outcome?: string | null
  /** Set when the resumed run immediately parked on a held action. */
  approval_id?: Uuid | null
  cost_usd?: number | null
}

/* ------------------------------------------------------------------ *
 * Devices (planned — see `Device` in entities.ts)
 * ------------------------------------------------------------------ */

/** `POST /me/devices`. Idempotent per `(user, token)`. */
export interface RegisterDeviceRequest {
  /** Expo / APNs / FCM token. */
  token: string
  platform: DevicePlatform
}

export interface RegisterDeviceResponse {
  ok: boolean
  device_id: Uuid
}

/**
 * The generic acknowledgement body. Used by deletes and other endpoints that
 * have nothing to return.
 */
export interface OkResponse {
  ok: boolean
  detail?: string | null
}

/* ------------------------------------------------------------------ *
 * Work items
 * ------------------------------------------------------------------ */

/**
 * `POST /work-items`.
 *
 * `reason` is not the item's description — `summary` is. It is why *this bot*
 * is the one holding it, and it lands on the opening row of the transfer
 * ledger so "who has held this" has no gap at the front.
 */
export interface CreateWorkItemRequest {
  owner_bot_id: Uuid
  title: string
  /** Free text; defaults to `"lead"`. */
  type?: string
  summary?: string
  status?: WorkItemStatus
  thread_id?: Uuid | null
  detail?: Record<string, unknown>
  keys?: WorkItemKey[]
  reason?: string
}

/**
 * `PATCH /work-items/{work_item_id}`. Unset fields are untouched.
 *
 * **Extras are rejected, not ignored** — this is the one input model in the API
 * that forbids unknown keys. Sending `owner_bot_id` is a 422 pointing at
 * `/work-items/{id}/transfer`, because a 200 that quietly dropped it would read
 * as a successful handover that produced no ledger row.
 *
 * `keys` replaces the whole set rather than merging, so a stale address can
 * actually be removed.
 */
export interface UpdateWorkItemRequest {
  title?: string
  summary?: string
  status?: WorkItemStatus
  resolution?: string | null
  thread_id?: Uuid | null
  detail?: Record<string, unknown>
  keys?: WorkItemKey[]
}

/**
 * `POST /work-items/{work_item_id}/transfer` → `WorkItemTransferResult`.
 *
 * `reason` is required and must be non-empty. Idempotent: transferring to the
 * bot that already holds the item answers `transferred: false` and writes
 * nothing.
 */
export interface TransferWorkItemRequest {
  to_bot_id: Uuid
  reason: string
  /** Set when a *bot* initiated the handover rather than the person at the keyboard. */
  actor_bot_id?: Uuid | null
  detail?: Record<string, unknown>
}

/**
 * `POST /inbound/sources` → `InboundSource`.
 *
 * `slug` is absent and cannot be supplied: it is the public path segment of the
 * hook URL and is generated server-side from a CSPRNG, so a caller-chosen value
 * would be both a cross-tenant name grab on a globally unique column and an
 * enumerable surface.
 *
 * A `webhook` source **must** carry `secret_ref` — an unsigned hook that starts
 * agent runs is a way to spend the owner's budget — and the value is validated
 * as a reference (`env://NAME`, `kv://vault/name`), never a key.
 */
export interface CreateInboundSourceRequest {
  name?: string
  kind?: InboundSourceKind
  /** The `WorkItemKey.channel` deliveries resolve against by default. */
  channel?: string
  bot_id?: Uuid | null
  /** Bots to seat when this lane has to create a thread for a reply. */
  bot_ids?: Uuid[]
  secret_ref?: string | null
  connector_id?: string | null
  config?: Record<string, unknown>
  enabled?: boolean
}

/**
 * `PATCH /inbound/sources/{source_id}` → `InboundSource`.
 *
 * Neither `slug` nor `kind` is editable. A hook URL that can be changed in place
 * is one that can be changed onto a value another tenant is about to be given,
 * and a half-migrated kind would be a poll holding a signing key or a webhook
 * without one. `enabled: false` is the kill switch: a disabled source refuses
 * every delivery with the answer an unknown slug gets.
 */
export interface UpdateInboundSourceRequest {
  name?: string
  channel?: string
  bot_id?: Uuid | null
  bot_ids?: Uuid[]
  secret_ref?: string | null
  connector_id?: string | null
  config?: Record<string, unknown>
  enabled?: boolean
}
