/**
 * Human takeover, normalised.
 *
 * An agent that hits a login, an MFA prompt or a CAPTCHA does not fail and does
 * not hand the task back as advice. It parks the run in `awaiting_human` and
 * waits for a person. That request reaches this app by two different routes:
 *
 *  - **live**, as a `takeover` SSE frame on whichever thread channel is open;
 *  - **parked**, from `GET /runs?status=awaiting_human`, which is what survives
 *    closing the app — and on a phone that is the *usual* route, because the
 *    app was almost certainly not running when the agent stopped.
 *
 * Both normalise to one {@link TakeoverRequest} so the inbox has a single shape
 * to render and so the same request arriving twice (live, then again from the
 * poll) is one row rather than two. `runId` is the identity: it is what
 * `POST /runs/{run_id}/resume` takes and it is stable across both routes.
 *
 * Nothing here reads or stores what the person types on the bot's desktop. The
 * only thing kept is the bot's own description of why it stopped.
 */
import type { DoneEventData, Run, TakeoverEventData } from "../api/types"

export interface TakeoverRequest {
  /** The run to resume. The identity of the request. */
  runId: string
  threadId: string | null
  botId: string | null
  /** Best known name; screens re-resolve it against the loaded bot list. */
  botName: string | null
  /** Why it stopped, in the bot's words. */
  reason: string
  /** What the person has to do before handing it back. */
  whatYouNeed: string
  /** When the agent asked, ISO-8601, or null when the row did not say. */
  askedAt: string | null
  /** How many times this run has already been resumed. */
  resumeCount: number
  /** What the bot was trying to achieve, when the parked state recorded it. */
  goal: string | null
  source: "live" | "parked"
}

/**
 * Shown when the agent gave no reason. Both halves are deliberately generic:
 * inventing a specific-sounding reason would put words in the bot's mouth on
 * the one screen where the person is about to act on them.
 */
const FALLBACK_REASON = "This task is waiting for you."
const FALLBACK_WHAT = "Finish the step on the bot's desktop, then press Continue."

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/** Trimmed non-empty string, or null. Keeps `""` out of the UI. */
function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null
}

/**
 * The agent's parked state, as `_park_agent_state` writes it.
 *
 * Everything needed to continue lives under `runs.detail.agent`, not in a
 * process, which is what lets the button be pressed an hour later from a
 * different device against a different API replica.
 */
function agentState(run: Run): Record<string, unknown> | null {
  const detail = run.detail
  if (!isRecord(detail)) return null
  const agent = detail["agent"]
  return isRecord(agent) ? agent : null
}

/**
 * A parked run as a takeover request, or `null` when the run is not one.
 *
 * Returns null rather than a half-filled card for a run whose status says
 * `awaiting_human` but which carries no agent state: that run is not resumable
 * (the API answers 409 `run_not_resumable`) and offering a Continue button for
 * it would be offering a button that cannot work.
 */
export function takeoverFromRun(run: Run): TakeoverRequest | null {
  if (run.status !== "awaiting_human") return null
  const agent = agentState(run)
  if (agent === null) return null

  const takeover = isRecord(agent["takeover"]) ? (agent["takeover"] as Record<string, unknown>) : null

  return {
    runId: run.id,
    threadId: run.thread_id ?? str(agent["thread_id"]),
    botId: run.bot_id ?? str(agent["bot_id"]),
    botName: null,
    reason: str(takeover?.["reason"]) ?? FALLBACK_REASON,
    whatYouNeed: str(takeover?.["what_you_need"]) ?? FALLBACK_WHAT,
    askedAt: str(takeover?.["asked_at"]) ?? str(agent["requested_at"]) ?? run.updated_at ?? run.created_at,
    resumeCount: typeof agent["resume_count"] === "number" ? agent["resume_count"] : 0,
    goal: str(agent["goal"]),
    source: "parked",
  }
}

/**
 * A live `takeover` frame as a request, or `null` when the frame is releasing
 * one rather than raising it.
 *
 * Only `phase: "requested"` raises. `"resumed"` — and any phase a later API
 * adds — must not mint a fresh card, which is why this returns null for
 * anything else instead of defaulting to "requested".
 */
export function takeoverFromEvent(data: TakeoverEventData): TakeoverRequest | null {
  if (data.phase !== "requested") return null
  const runId = str(data.run_id)
  if (!runId) return null

  return {
    runId,
    threadId: str(data.thread_id),
    botId: str(data.bot_id),
    botName: str(data.bot_name),
    reason: str(data.reason) ?? FALLBACK_REASON,
    whatYouNeed: str(data.what_you_need) ?? FALLBACK_WHAT,
    askedAt: new Date().toISOString(),
    resumeCount: 0,
    goal: null,
    source: "live",
  }
}

/**
 * The last-resort route: a `done` frame that says the turn parked on a person.
 *
 * A client that connected part-way through a turn never saw the `takeover`
 * frame, but every `done` carries `run_id` and `awaiting_human` precisely so
 * that this case is recoverable. Without it the person is left reading a reply
 * that asks for help with no way to give it, which is the worst version of this
 * feature — the bot asked, and the app swallowed it.
 *
 * The request it builds is deliberately thin: the `done` frame does not carry
 * the reason or the instruction, so those fall back to the generic text and the
 * real ones arrive when the takeover screen loads the run.
 */
export function takeoverFromDone(data: DoneEventData, botName?: string | null): TakeoverRequest | null {
  if (data.awaiting_human !== true) return null
  const runId = str(data.run_id)
  if (!runId) return null

  return {
    runId,
    threadId: str(data.thread_id),
    botId: str(data.bot_id),
    botName: botName ?? str(data.bot_name),
    reason: FALLBACK_REASON,
    whatYouNeed: FALLBACK_WHAT,
    askedAt: new Date().toISOString(),
    resumeCount: 0,
    goal: null,
    source: "live",
  }
}

/**
 * Merge two lists of requests, keyed on `runId`, newest first.
 *
 * A live frame wins over a parked row for the same run: it is more recent and
 * it carries the bot's name, which the parked row does not.
 */
export function mergeTakeovers(existing: readonly TakeoverRequest[], incoming: readonly TakeoverRequest[]) {
  // Built inside the function, never captured from outside: a Map created in an
  // enclosing scope and mutated here would be drained on React's second
  // StrictMode pass, and the bug would be invisible in development by
  // construction.
  const byRun = new Map<string, TakeoverRequest>()
  for (const item of existing) byRun.set(item.runId, item)
  for (const item of incoming) {
    const previous = byRun.get(item.runId)
    byRun.set(
      item.runId,
      previous && item.source === "parked" ? { ...item, botName: item.botName ?? previous.botName } : item,
    )
  }
  return [...byRun.values()].sort((a, b) => String(b.askedAt ?? "").localeCompare(String(a.askedAt ?? "")))
}
