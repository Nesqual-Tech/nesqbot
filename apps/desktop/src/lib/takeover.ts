/**
 * Human handoff: reading a `takeover` off the wire, and remembering one.
 *
 * The API parks a run in `awaiting_human` when the agent hits a login, an MFA
 * prompt or a CAPTCHA. Two things reach the client:
 *
 *  - a **`takeover` SSE event**, the instant it happens, on whichever thread
 *    channel is open;
 *  - the **run itself**, from `GET /runs?status=awaiting_human`, which is what
 *    survives closing the app.
 *
 * Both normalise to one {@link TakeoverRequest} so the UI has a single shape to
 * render, and so a run that arrives twice (live, then again from the poll) is
 * one card rather than two.
 *
 * Nothing in this module touches what the person types on the bot's desktop.
 * The only thing cached to disk is the bot's own description of why it stopped.
 */
import type { ChannelEvent, DoneEventData, ParkedRun, TakeoverEventData } from "../types"

/** The run status the API parks a handed-off run in. Not in `RunStatus` yet. */
export const AWAITING_HUMAN = "awaiting_human"

/** The only phase that raises a request. Anything else resolves one. */
export const TAKEOVER_REQUESTED = "requested"

/**
 * One thing the person has to finish before an agent can carry on.
 *
 * `runId` is the identity — it is what `POST /runs/{run_id}/resume` takes, and
 * it is stable across the live event, a reload and the poll.
 */
export interface TakeoverRequest {
  runId: string
  threadId: string | null
  botId: string | null
  /** Best known name; the shell re-resolves it against the loaded bot list. */
  botName: string
  /** Why the agent stopped, in its own words. */
  reason: string
  /** What it needs from the person. */
  whatYouNeed: string
  /** When this client first learned about it. */
  raisedAt: number
  /** `live` arrived on a stream; `restored` came back from the parked-run list. */
  source: "live" | "restored"
}

const FALLBACK_REASON = "This task is waiting for you."
const FALLBACK_WHAT = "Finish the step on the bot's desktop, then press Continue."

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null
}

/* ------------------------------------------------------------------ *
 * Off the wire
 * ------------------------------------------------------------------ */

/**
 * Narrow a channel frame to a `takeover`, or `null`.
 *
 * Accepts two spellings on purpose. Today `@nesqbot/protocol` has no
 * `takeover` arm, so `parseThreadEvent`/`parseSseEvent` hand the frame back as
 * `{event: "unknown", name: "takeover"}` — which is exactly the forward
 * compatibility that arm exists for. When the package grows a real arm the
 * frame will arrive as `{event: "takeover"}` instead, and this keeps working
 * without a flag day.
 */
export function parseTakeoverEvent(event: ChannelEvent): TakeoverEventData | null {
  const named = event as { event: string; name?: string; data?: unknown }
  const name = named.event === "unknown" ? named.name : named.event
  if (name !== "takeover") return null

  const data = named.data
  if (!isRecord(data)) return null

  const runId = str(data["run_id"])
  if (!runId) return null

  return {
    phase: str(data["phase"]) ?? TAKEOVER_REQUESTED,
    run_id: runId,
    thread_id: str(data["thread_id"]),
    bot_id: str(data["bot_id"]),
    bot_name: str(data["bot_name"]),
    reason: str(data["reason"]),
    what_you_need: str(data["what_you_need"]),
    resume_url: str(data["resume_url"]),
  }
}

/** True when the frame is asking for a person, rather than releasing one. */
export function isTakeoverRequested(data: TakeoverEventData): boolean {
  return data.phase === TAKEOVER_REQUESTED
}

/**
 * `run_id` and `awaiting_human` off a `done` frame.
 *
 * The API added both, and `DoneEventData` in `@nesqbot/protocol` does not
 * declare either — the parser keeps unknown keys, so they are there to be read.
 * A `done` that says `awaiting_human` is a turn that ended by parking; a `done`
 * that names a run we are holding and does *not* say so is that run finishing,
 * and the card should go away.
 */
export function readDoneRun(data: DoneEventData): { runId: string | null; awaitingHuman: boolean } {
  const record = data as unknown as Record<string, unknown>
  return {
    runId: str(record["run_id"]),
    awaitingHuman: record["awaiting_human"] === true,
  }
}

/* ------------------------------------------------------------------ *
 * Normalising
 * ------------------------------------------------------------------ */

export function requestFromEvent(data: TakeoverEventData, fallbackThreadId?: string | null): TakeoverRequest {
  return {
    runId: data.run_id,
    threadId: data.thread_id ?? fallbackThreadId ?? null,
    botId: data.bot_id ?? null,
    botName: data.bot_name ?? "Your teammate",
    reason: data.reason ?? FALLBACK_REASON,
    whatYouNeed: data.what_you_need ?? FALLBACK_WHAT,
    raisedAt: Date.now(),
    source: "live",
  }
}

/**
 * A parked run from `GET /runs?status=awaiting_human`.
 *
 * The wire type for `Run.detail` is an open `Record`, so where the orchestrator
 * writes the handoff copy is read defensively — top level and under a
 * `takeover` key both work, and neither existing is survivable: the remembered
 * copy fills in, and failing that the generic sentence does. What must never
 * happen is a parked run that cannot be found because its `detail` shape moved.
 */
export function requestFromRun(run: ParkedRun): TakeoverRequest {
  const detail = isRecord(run.detail) ? run.detail : {}
  const nested = isRecord(detail["takeover"]) ? (detail["takeover"] as Record<string, unknown>) : detail

  return {
    runId: run.id,
    threadId: run.thread_id ?? null,
    botId: run.bot_id ?? null,
    botName: str(nested["bot_name"]) ?? "Your teammate",
    reason: str(nested["reason"]) ?? FALLBACK_REASON,
    whatYouNeed: str(nested["what_you_need"]) ?? FALLBACK_WHAT,
    raisedAt: Date.parse(run.created_at) || Date.now(),
    source: "restored",
  }
}

/**
 * Merge what the server just said with what we already had.
 *
 * The server list is authoritative about *which* runs are parked; the live
 * event is usually better about *why*. So identity and status come from
 * `next`, and the human-readable copy is only overwritten when the incoming
 * value is a real one rather than the generic fallback.
 */
export function mergeRequest(previous: TakeoverRequest | undefined, next: TakeoverRequest): TakeoverRequest {
  if (!previous) return next
  const better = (incoming: string, held: string, fallback: string): string => (incoming !== fallback ? incoming : held)
  return {
    ...next,
    threadId: next.threadId ?? previous.threadId,
    botId: next.botId ?? previous.botId,
    botName: next.botName !== "Your teammate" ? next.botName : previous.botName,
    reason: better(next.reason, previous.reason, FALLBACK_REASON),
    whatYouNeed: better(next.whatYouNeed, previous.whatYouNeed, FALLBACK_WHAT),
    raisedAt: Math.min(previous.raisedAt, next.raisedAt),
    // A run we saw live stays "live" — it is the same interruption, and the
    // shell uses this to decide whether it has already been put in front of
    // the person.
    source: previous.source === "live" ? "live" : next.source,
  }
}

/* ------------------------------------------------------------------ *
 * Remembering the copy across a restart
 *
 * The run itself survives on the server; the sentence explaining it might not,
 * if the orchestrator's `detail` does not carry it. Caching the two strings
 * means someone who reopens the app sees "LinkedIn is asking for a password"
 * rather than a generic placeholder over a resume button.
 *
 * Only the bot's own words are stored. Never a note, never a keystroke.
 * ------------------------------------------------------------------ */

const COPY_KEY = "nesq.takeover.copy"
const COPY_LIMIT = 12
const COPY_TTL_MS = 7 * 24 * 60 * 60 * 1000

interface StoredCopy {
  threadId: string | null
  botId: string | null
  botName: string
  reason: string
  whatYouNeed: string
  raisedAt: number
}

type CopyMap = Record<string, StoredCopy>

function readCopyMap(): CopyMap {
  try {
    const raw = localStorage.getItem(COPY_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    return isRecord(parsed) ? (parsed as CopyMap) : {}
  } catch {
    return {}
  }
}

function writeCopyMap(map: CopyMap): void {
  try {
    localStorage.setItem(COPY_KEY, JSON.stringify(map))
  } catch {
    /* a full or blocked store costs a sentence, not the feature */
  }
}

/** Remember one request's copy. Prunes expired and surplus entries as it goes. */
export function rememberCopy(request: TakeoverRequest): void {
  const map = readCopyMap()
  map[request.runId] = {
    threadId: request.threadId,
    botId: request.botId,
    botName: request.botName,
    reason: request.reason,
    whatYouNeed: request.whatYouNeed,
    raisedAt: request.raisedAt,
  }

  const cutoff = Date.now() - COPY_TTL_MS
  const kept = Object.entries(map)
    .filter(([, value]) => isRecord(value) && typeof value.raisedAt === "number" && value.raisedAt > cutoff)
    .sort((a, b) => b[1].raisedAt - a[1].raisedAt)
    .slice(0, COPY_LIMIT)

  writeCopyMap(Object.fromEntries(kept))
}

/** Fill a restored request back in from the remembered copy, where there is any. */
export function applyRememberedCopy(request: TakeoverRequest): TakeoverRequest {
  const stored = readCopyMap()[request.runId]
  if (!isRecord(stored)) return request
  return mergeRequest(
    {
      runId: request.runId,
      threadId: (stored.threadId as string | null) ?? null,
      botId: (stored.botId as string | null) ?? null,
      botName: str(stored.botName) ?? "Your teammate",
      reason: str(stored.reason) ?? FALLBACK_REASON,
      whatYouNeed: str(stored.whatYouNeed) ?? FALLBACK_WHAT,
      raisedAt: typeof stored.raisedAt === "number" ? stored.raisedAt : request.raisedAt,
      source: "restored",
    },
    request,
  )
}

/** Drop a run's remembered copy once it is no longer parked. */
export function forgetCopy(runId: string): void {
  const map = readCopyMap()
  if (!(runId in map)) return
  delete map[runId]
  writeCopyMap(map)
}
