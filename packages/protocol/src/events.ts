/**
 * Server-sent events — two channels, two unions.
 *
 * The API exposes the same turn over two SSE endpoints, and they do NOT carry
 * the same events. Conflating them is the bug this file exists to prevent.
 *
 *   POST /threads/{id}/messages/stream   -> TurnStreamEvent
 *     The requester's own response. Carries `token` deltas as the model
 *     produces them. Never carries `turn_started` — you started the turn.
 *
 *   GET  /threads/{id}/events            -> ThreadEvent
 *     A passive subscription for anyone else watching the thread (a second
 *     window, the other device, a worker-driven routine turn). Never carries
 *     `token` — a per-character Redis publish is real load for no benefit —
 *     so it gets `turn_started` for the typing indicator and the finished
 *     text on `done` instead.
 *
 * Both are produced by `Orchestrator._emit` in
 * `apps/api/app/services/orchestrator.py`, which yields to the caller and
 * fans out to subscribers, skipping `token` on the fan-out. That is why the
 * payload for a shared event name is identical on both channels.
 */

import type { ModelTier, Uuid } from "./core"

/* ------------------------------------------------------------------ *
 * Event payloads
 *
 * Names match what the desktop and mobile lanes settled on. The older
 * `Sse*Data` spellings are kept as aliases at the end of this section.
 * ------------------------------------------------------------------ */

/** Incremental assistant text. Append `delta`; do not re-render from scratch. */
export interface TokenEventData {
  delta: string
}

/**
 * Passive channel only: a turn just began on this thread. Drive the typing
 * indicator from this — it is the substitute for the `token` deltas the
 * passive channel deliberately does not carry.
 */
export interface TurnStartedEventData {
  thread_id: Uuid
  bot_id: Uuid
  bot_name: string
}

/**
 * A different bot is answering now.
 *
 * **One event, two mechanisms**, and that is deliberate (see "Bots working
 * together" in `docs/API.md`):
 *
 * - **Routing** picks which bot answers one human message. Nothing is handed
 *   over — no brief, no ownership — and only `bot_id`/`bot_name` are set.
 * - **Delegation** is a bot handing work to another bot through the
 *   `delegate_to_bot` control tool. It carries a brief and, where a work item
 *   exists, its ownership, and it sets the five keys below.
 *
 * A client reading only `bot_id`/`bot_name` cannot tell them apart and does not
 * need to. Read `delegated` when you do — for instance to render
 * "Lead Generator → Sales" instead of a bare "handed off to Sales", which is
 * the difference between showing a team working and showing a topic change.
 */
export interface HandoffEventData {
  /** The bot now answering. */
  bot_id: Uuid
  bot_name: string
  /** The bot that handed the work over. Delegation only. */
  from_bot_id?: Uuid
  from_bot_name?: string
  /** The delegated run — what a resume or an audit query is addressed by. */
  run_id?: Uuid
  /** The delegation path so far, e.g. `person → lead_generator → sales`. */
  chain?: string
  /**
   * `true` on the delegated path. Absent on the routing path — do not read a
   * missing value as a checked `false`; it means "this was routing".
   */
  delegated?: boolean
}

/** A connector action or MCP tool ran (or failed) mid-turn. */
export interface ToolEventData {
  connector: string
  action: string
  ok: boolean
}

/**
 * Either the turn parked on a human decision, or a decision just released one.
 *
 * The bare `{approval_id, title}` frame means **parked**: an action was held and
 * the run is now `awaiting_approval`. Stop expecting tokens.
 *
 * `phase` appears only on the other kind of frame — a decision that resumed a
 * parked run — and says which way it went. The API reuses this event name for
 * the continuation rather than inventing one, so a client that already renders
 * "approval needed" does not break; the price is that a client wanting to tell
 * "held" from "released" must read `phase`, not just the event name.
 */
export interface ApprovalEventData {
  approval_id: Uuid
  title: string
  /** Set only when a decision is resuming a parked run. */
  phase?: "approved" | "rejected"
  /** The run being continued. Set alongside `phase`. */
  run_id?: Uuid
}

/**
 * Terminal event. Two different frames share this name:
 *
 *  1. **Turn finished** — carries `message_id`, `bot_id`, `bot_name`, the full
 *     text, `tier` and `cost_usd`. Emitted on both channels.
 *  2. **Stream closed** — carries only `thread_id` (plus `reason: "closed"` on
 *     the passive channel). Emitted by the endpoint's `finally` block when the
 *     connection ends without a terminal event, so a reader always sees a
 *     close-out. Nothing was completed.
 *
 * `isStreamClosedDone()` tells them apart. Every field is optional because no
 * single field appears on both shapes.
 */
export interface DoneEventData {
  message_id?: Uuid
  bot_id?: Uuid
  bot_name?: string
  /**
   * The full final message text.
   *
   * **This is the wire field — read it, not `content`.** The passive channel
   * needs it because its subscribers never saw the token deltas; the turn
   * stream sends it too and the requester can ignore it. Prefer
   * `doneEventText()` over touching either field directly.
   */
  message?: string
  /**
   * Accepted alias for `message`. The API does **not** emit this today —
   * `apps/api/app/services/orchestrator.py` writes `message`. It is declared
   * only so client code written against the earlier local guess keeps
   * compiling. Do not read it directly; use `doneEventText()`.
   */
  content?: string
  /** Null on a budget-blocked turn, where no model was called. */
  tier?: ModelTier | null
  cost_usd?: number
  /** Set when the turn ended by parking on an approval rather than finishing. */
  approval_id?: Uuid | null
  /** True when the bot refused to spend past its daily cap. */
  budget_blocked?: boolean
  /**
   * The run behind the turn.
   *
   * Emitted so a client that only ever sees SSE can still find the run — which
   * matters because `POST /runs/{run_id}/resume` is addressed by run id, and a
   * parked turn's `done` is where that id first reaches the client.
   */
  run_id?: Uuid
  /**
   * `true` when the turn ended by **parking on a person** rather than by
   * finishing. A `takeover` frame carries the details; this flag is what lets a
   * client that missed it (reconnected mid-turn) avoid rendering the message as
   * a completed answer. A later `done` naming the same run *without* the flag
   * is that run finishing.
   */
  awaiting_human?: boolean
  /** Present on the close-out frame only. */
  thread_id?: Uuid
  /** `"closed"` on the passive channel's close-out frame. */
  reason?: string
}

/**
 * The agent hit something only a person can do — a login, an MFA prompt, a
 * CAPTCHA — and handed the screen over.
 *
 * This is **not** a failure and not the end of the task. The run parks in
 * `awaiting_human` with everything needed to continue persisted on the row, so
 * the button may be pressed an hour later, from a different device, against a
 * different API replica. The client shows the live desktop, lets the person do
 * the sensitive step themselves, and calls `POST /runs/{run_id}/resume`; the
 * agent then takes a fresh screenshot to see what changed and carries on with
 * the original task.
 *
 * `run_id` is the identity — it is what resume takes, and it is stable across
 * the live event, a reload, and `GET /runs?status=awaiting_human`.
 */
export interface TakeoverEventData {
  /** `"requested"` raises one; `"resumed"` is the frame that clears it. */
  phase: string
  run_id: Uuid
  thread_id?: Uuid | null
  bot_id?: Uuid | null
  bot_name?: string | null
  /** Why it stopped, in the bot's words: "LinkedIn is asking for a password". */
  reason?: string | null
  /** What the person must do: "Sign in, then press continue". */
  what_you_need?: string | null
  /**
   * Server-supplied convenience (`/api/runs/{id}/resume`). Prefer building the
   * request from `run_id` against your own base URL — this string carries the
   * API's own mount prefix, and appending it to a base that already has one
   * produces `/api/api/…`.
   */
  resume_url?: string | null
}

/** How far along a bot's desktop is during a turn. */
export type DesktopEventPhase = "starting" | "ready" | "unavailable" | "blocked" | "finished"

/**
 * Progress while a bot drives its Bot Desktop.
 *
 * Exists because a cold start on ACI takes 30–90 seconds, during which a client
 * with no `desktop` handling shows a turn that looks hung. `starting` repeats
 * with a rising `elapsed_seconds`, so treat it as a progress feed rather than a
 * one-shot.
 *
 * Individual desktop actions are **not** here — they arrive as ordinary `tool`
 * frames with `connector: "desktop"`.
 */
export interface DesktopEventData {
  bot_id: Uuid
  phase: DesktopEventPhase
  /** Human-readable: why it will not start, why it is gated, how long so far. */
  detail?: string | null
  /** Seconds since the boot began. Repeats on `starting`. */
  elapsed_seconds?: number
  /** `ready` only. */
  state?: string | null
  /** `finished` only: how many desktop actions actually ran. */
  steps?: number
  /** `finished` only: how the agent loop ended. */
  outcome?: string | null
  /** `finished` only: set when a step was held for a human instead of running. */
  approval_id?: Uuid | null
}

/**
 * What one step of an agent loop just cost, emitted as it happens.
 *
 * A single autonomous turn can consume a day's budget, and the failure this
 * event exists to prevent is finding that out afterwards. `image_tokens`
 * against `input_tokens` is the whole story of a vision loop;
 * `spent_today_usd` against `budget_usd` is how much room is left.
 */
export interface CostEventData {
  bot_id: Uuid
  run_id: Uuid
  /** Which step of the loop, 1-based. */
  step: number
  tier?: ModelTier | null
  input_tokens: number
  image_tokens: number
  output_tokens: number
  /** This step alone. */
  cost_usd: number
  /** The whole turn so far. */
  turn_cost_usd: number
  spent_today_usd: number
  budget_usd: number
}

/** Terminal failure. Matches the body of the HTTP error envelope. */
export interface ErrorEventData {
  detail: string
  code?: string
}

/**
 * Any frame this build does not recognise: an event name added by a newer API,
 * or a known name whose payload did not match.
 *
 * Both channel unions carry this arm on purpose. Without it an exhaustive
 * `switch` in a client rejects every event name the server might add later,
 * which turns "ship a new server event" into "break the deployed clients".
 * With it, an unrecognised frame is a case you can ignore rather than a crash.
 *
 * `name` is the raw event name off the wire; `data` is the parsed JSON when it
 * parsed, and the raw string when it did not.
 */
export interface UnknownChannelEvent {
  event: "unknown"
  name: string
  data: unknown
}

/** @deprecated Use {@link TokenEventData}. */
export type SseTokenData = TokenEventData
/** @deprecated Use {@link HandoffEventData}. */
export type SseHandoffData = HandoffEventData
/** @deprecated Use {@link ToolEventData}. */
export type SseToolData = ToolEventData
/** @deprecated Use {@link ApprovalEventData}. */
export type SseApprovalData = ApprovalEventData
/** @deprecated Use {@link DoneEventData}. */
export type SseDoneData = DoneEventData
/** @deprecated Use {@link ErrorEventData}. */
export type SseErrorData = ErrorEventData

/* ------------------------------------------------------------------ *
 * Channel unions
 * ------------------------------------------------------------------ */

/** Event names on `POST /threads/{id}/messages/stream`. */
export type TurnStreamEventName =
  "token" | "handoff" | "tool" | "approval" | "desktop" | "takeover" | "cost" | "done" | "error"

/**
 * Event names on `GET /threads/{id}/events`.
 *
 * Mirrors `PUBLISHED_EVENTS` in `apps/api/app/services/orchestrator.py`, which
 * is the fan-out allowlist: everything the turn stream carries except `token`,
 * plus `turn_started`.
 */
export type ThreadEventName =
  "turn_started" | "handoff" | "tool" | "approval" | "desktop" | "takeover" | "cost" | "done" | "error"

/**
 * Every name either channel can produce **on the wire**. Deliberately excludes
 * `"unknown"`, which is a client-side arm rather than something the API sends
 * (see {@link UnknownChannelEvent}).
 */
export type ChannelEventName = TurnStreamEventName | ThreadEventName

export const TURN_STREAM_EVENT_NAMES = [
  "token",
  "handoff",
  "tool",
  "approval",
  "desktop",
  "takeover",
  "cost",
  "done",
  "error",
] as const satisfies readonly TurnStreamEventName[]

export const THREAD_EVENT_NAMES = [
  "turn_started",
  "handoff",
  "tool",
  "approval",
  "desktop",
  "takeover",
  "cost",
  "done",
  "error",
] as const satisfies readonly ThreadEventName[]

/**
 * The turn stream: what your own `POST .../messages/stream` response carries.
 * `token` is here; `turn_started` is not.
 */
export type TurnStreamEvent =
  | { event: "token"; data: TokenEventData }
  | { event: "handoff"; data: HandoffEventData }
  | { event: "tool"; data: ToolEventData }
  | { event: "approval"; data: ApprovalEventData }
  | { event: "desktop"; data: DesktopEventData }
  | { event: "takeover"; data: TakeoverEventData }
  | { event: "cost"; data: CostEventData }
  | { event: "done"; data: DoneEventData }
  | { event: "error"; data: ErrorEventData }
  | UnknownChannelEvent

/**
 * The passive channel: what `GET .../events` carries for someone watching a
 * thread they did not start. `turn_started` is here; `token` is not.
 */
export type ThreadEvent =
  | { event: "turn_started"; data: TurnStartedEventData }
  | { event: "handoff"; data: HandoffEventData }
  | { event: "tool"; data: ToolEventData }
  | { event: "approval"; data: ApprovalEventData }
  | { event: "desktop"; data: DesktopEventData }
  | { event: "takeover"; data: TakeoverEventData }
  | { event: "cost"; data: CostEventData }
  | { event: "done"; data: DoneEventData }
  | { event: "error"; data: ErrorEventData }
  | UnknownChannelEvent

/** Either channel. The guards below accept this so one handler can serve both. */
export type ChannelEvent = TurnStreamEvent | ThreadEvent

/**
 * @deprecated Ambiguous — both channels are SSE. Use {@link TurnStreamEvent}
 * for `POST .../messages/stream` or {@link ThreadEvent} for `GET .../events`.
 * Kept because app code already imports this name; it means the turn stream.
 */
export type SseEvent = TurnStreamEvent

/** @deprecated Use {@link TurnStreamEventName}. */
export type SseEventName = TurnStreamEventName

/** @deprecated Use {@link TURN_STREAM_EVENT_NAMES}. */
export const SSE_EVENT_NAMES = TURN_STREAM_EVENT_NAMES

/** Payload type for a given event name, on either channel. */
export type ChannelEventData<N extends ChannelEventName> = Extract<ChannelEvent, { event: N }>["data"]

/** @deprecated Use {@link ChannelEventData}. */
export type SseEventData<N extends TurnStreamEventName> = Extract<TurnStreamEvent, { event: N }>["data"]

/** Events after which the channel will send nothing further. */
export const TERMINAL_SSE_EVENTS = ["done", "error"] as const satisfies readonly ChannelEventName[]

export function isTerminalSseEvent(event: ChannelEvent): boolean {
  return (TERMINAL_SSE_EVENTS as readonly string[]).includes(event.event)
}

/* ------------------------------------------------------------------ *
 * Guards
 * ------------------------------------------------------------------ */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function isTurnStreamEventName(value: unknown): value is TurnStreamEventName {
  return typeof value === "string" && (TURN_STREAM_EVENT_NAMES as readonly string[]).includes(value)
}

export function isThreadEventName(value: unknown): value is ThreadEventName {
  return typeof value === "string" && (THREAD_EVENT_NAMES as readonly string[]).includes(value)
}

/** @deprecated Use {@link isTurnStreamEventName}. */
export const isSseEventName = isTurnStreamEventName

/** True for a frame this build did not recognise. Handle or ignore; never throw. */
export function isUnknownEvent(event: ChannelEvent): event is UnknownChannelEvent {
  return event.event === "unknown"
}

export function isTokenEvent(event: ChannelEvent): event is Extract<TurnStreamEvent, { event: "token" }> {
  return event.event === "token"
}

export function isTurnStartedEvent(event: ChannelEvent): event is Extract<ThreadEvent, { event: "turn_started" }> {
  return event.event === "turn_started"
}

export function isHandoffEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "handoff" }> {
  return event.event === "handoff"
}

export function isToolEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "tool" }> {
  return event.event === "tool"
}

export function isApprovalEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "approval" }> {
  return event.event === "approval"
}

export function isDesktopEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "desktop" }> {
  return event.event === "desktop"
}

export function isTakeoverEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "takeover" }> {
  return event.event === "takeover"
}

export function isCostEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "cost" }> {
  return event.event === "cost"
}

/**
 * True when a `takeover` frame is *asking* for a person rather than releasing
 * one. `phase` is a plain string because the API may add phases, and an
 * unrecognised one must not be treated as a fresh request.
 */
export function isTakeoverRequested(data: TakeoverEventData): boolean {
  return data.phase === "requested"
}

export function isDoneEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "done" }> {
  return event.event === "done"
}

export function isErrorEvent(event: ChannelEvent): event is Extract<ChannelEvent, { event: "error" }> {
  return event.event === "error"
}

/**
 * True for the close-out `done` an endpoint emits when the connection ends
 * without a terminal event. Nothing finished — do not render it as a reply.
 */
export function isStreamClosedDone(data: DoneEventData): boolean {
  return data.message_id === undefined && data.thread_id !== undefined
}

/**
 * The finished assistant text from a `done` event, or `null` when there is
 * none (a close-out frame, or a turn that produced no text).
 *
 * Reads the wire field `message` and falls back to `content`, so callers never
 * have to remember which spelling they are looking at.
 */
export function doneEventText(data: DoneEventData): string | null {
  return data.message ?? data.content ?? null
}

/** Shared payload validation for one `{event, data}` pair. */
function isValidPayload(name: string, data: Record<string, unknown>): boolean {
  switch (name) {
    case "token":
      return typeof data["delta"] === "string"
    case "turn_started":
      return typeof data["bot_id"] === "string" && typeof data["bot_name"] === "string"
    case "handoff":
      return typeof data["bot_id"] === "string" && typeof data["bot_name"] === "string"
    case "tool":
      return (
        typeof data["connector"] === "string" && typeof data["action"] === "string" && typeof data["ok"] === "boolean"
      )
    case "approval":
      return typeof data["approval_id"] === "string" && typeof data["title"] === "string"
    case "desktop":
      return typeof data["bot_id"] === "string" && typeof data["phase"] === "string"
    // `run_id` is the whole point of the frame — it is what resume is addressed
    // by — so a takeover without one is unusable and routes to `unknown`.
    case "takeover":
      return typeof data["run_id"] === "string" && typeof data["phase"] === "string"
    case "cost":
      return typeof data["run_id"] === "string" && typeof data["cost_usd"] === "number"
    case "done":
      // Both the turn-finished and the stream-closed shapes are valid.
      return typeof data["message_id"] === "string" || typeof data["thread_id"] === "string"
    case "error":
      return typeof data["detail"] === "string"
    default:
      return false
  }
}

function isUnknownArm(value: Record<string, unknown>): boolean {
  return value["event"] === "unknown" && typeof value["name"] === "string"
}

/** Structural check on an already-assembled turn-stream event. */
export function isSseEvent(value: unknown): value is TurnStreamEvent {
  if (!isRecord(value)) return false
  if (isUnknownArm(value)) return true
  if (!isTurnStreamEventName(value["event"]) || !isRecord(value["data"])) return false
  return isValidPayload(value["event"], value["data"])
}

/** Structural check on an already-assembled passive-channel event. */
export function isThreadEvent(value: unknown): value is ThreadEvent {
  if (!isRecord(value)) return false
  if (isUnknownArm(value)) return true
  if (!isThreadEventName(value["event"]) || !isRecord(value["data"])) return false
  return isValidPayload(value["event"], value["data"])
}

/** Parse the `data:` line, keeping the raw string when it is not JSON. */
function parseFrame(raw: string | unknown): { data: unknown; record: Record<string, unknown> | null } {
  let data: unknown = raw
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw)
    } catch {
      return { data: raw, record: null }
    }
  }
  return { data, record: isRecord(data) ? data : null }
}

/**
 * Turn one raw frame from `POST .../messages/stream` into a typed event.
 *
 * `raw` is the `data:` line — the JSON string off the wire, or an already
 * parsed object.
 *
 * Never returns `null` and never throws. An unrecognised name, unparseable
 * JSON, or a payload that does not match the contract all come back as
 * {@link UnknownChannelEvent} with the original name and whatever data
 * survived, so a reader can log or ignore the frame — and so an event added by
 * a newer API does not break a deployed client.
 */
export function parseSseEvent(name: string, raw: string | unknown): TurnStreamEvent {
  const { data, record } = parseFrame(raw)
  if (!isTurnStreamEventName(name) || record === null || !isValidPayload(name, record)) {
    return { event: "unknown", name, data }
  }
  return { event: name, data: record } as TurnStreamEvent
}

/**
 * Turn one raw frame from `GET .../events` into a typed event.
 *
 * Same forward-compatible contract as {@link parseSseEvent}: anything this
 * build does not recognise arrives as {@link UnknownChannelEvent} rather than
 * being dropped. That includes `token`, which this channel never sends — a
 * frame claiming otherwise is surfaced, not rendered.
 */
export function parseThreadEvent(name: string, raw: string | unknown): ThreadEvent {
  const { data, record } = parseFrame(raw)
  if (!isThreadEventName(name) || record === null || !isValidPayload(name, record)) {
    return { event: "unknown", name, data }
  }
  return { event: name, data: record } as ThreadEvent
}
