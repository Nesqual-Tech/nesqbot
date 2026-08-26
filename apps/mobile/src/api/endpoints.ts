/**
 * Every API route the mobile app touches, typed against docs/API.md.
 *
 * Grouped by resource so screens read as `api.approvals.decide(...)` rather than
 * assembling paths by hand.
 */
import type {
  Approval,
  ApprovalDecisionResult,
  ApprovalStatus,
  Bot,
  BotDesktop,
  DesktopActionInput,
  DesktopActionOutcome,
  DesktopScreenshot,
  DesktopStreamTicket,
  DesktopWindowsResult,
  DeviceRegistration,
  HealthOut,
  Message,
  ProvidersOut,
  ResumeRunOut,
  Run,
  RunStatus,
  Thread,
  TokenOut,
  UpdateBotInput,
  UsageOut,
  User,
  WorkItem,
  WorkItemStatus,
  WorkItemTransfer,
  WorkItemTransferResult,
} from "./types"
import { request, type RequestOptions } from "./client"
import { openEventStream, type EventStreamHandle } from "./sse"
import {
  isStreamClosedDone,
  parseSseEvent,
  parseThreadEvent,
  type ThreadEvent,
  type TurnStreamEvent,
} from "@nesqbot/protocol"

type Opts = Pick<RequestOptions, "signal" | "timeoutMs">

/* ------------------------------------------------------------------- health */

export const health = {
  shallow: (opts?: Opts): Promise<HealthOut> => request<HealthOut>("/health", { ...opts }),
}

/* --------------------------------------------------------------------- auth */

export const auth = {
  /** Dev-only sign-in; the API returns 403 when NESQ_ENV=production. */
  devLogin: (opts?: Opts): Promise<TokenOut> =>
    request<TokenOut>("/auth/dev-login", { method: "POST", body: {}, ...opts }),

  /**
   * Exchanges a Microsoft Entra **access token** for a Nesq Bot session token.
   *
   * Sent as a bearer credential rather than in the body, which is where the API
   * reads it from. The API re-validates it against the tenant JWKS (audience,
   * issuer, expiry, `scp`) before it mints anything.
   */
  entra: (entraAccessToken: string, opts?: Opts): Promise<TokenOut> =>
    request<TokenOut>("/auth/entra", {
      method: "POST",
      headers: { Authorization: `Bearer ${entraAccessToken}` },
      ...opts,
    }),

  me: (opts?: Opts): Promise<User> => request<User>("/me", { ...opts }),
}

/* --------------------------------------------------------------------- bots */

export const bots = {
  list: (opts?: Opts): Promise<Bot[]> => request<Bot[]>("/bots", { ...opts }),
  get: (botId: string, opts?: Opts): Promise<Bot> => request<Bot>(`/bots/${botId}`, { ...opts }),
  setBudget: (botId: string, dailyBudgetUsd: number, opts?: Opts): Promise<Bot> =>
    request<Bot>(`/bots/${botId}/budget`, {
      method: "PATCH",
      body: { daily_budget_usd: dailyBudgetUsd },
      ...opts,
    }),
  /**
   * Pin (or clear) which provider/model this bot talks to for every task it
   * runs, bypassing the router's tier system. `null` on both fields clears
   * the override and reverts to tier routing; one without the other is a 422
   * (`incomplete_model_override`) — see `apps/api/app/routers/bots.py`.
   */
  setModel: (botId: string, input: Pick<UpdateBotInput, "model_provider" | "model_name">, opts?: Opts): Promise<Bot> =>
    request<Bot>(`/bots/${botId}`, { method: "PATCH", body: input, ...opts }),
  /** Which providers this deployment can actually reach right now — a live credential resolved, not just a name this build recognises. */
  providers: (opts?: Opts): Promise<ProvidersOut> => request<ProvidersOut>("/bots/providers", { ...opts }),
}

/* ---------------------------------------------------------- threads/messages */

export const threads = {
  list: (opts?: Opts): Promise<Thread[]> => request<Thread[]>("/threads", { ...opts }),

  create: (botIds: string[], title?: string, opts?: Opts): Promise<Thread> =>
    request<Thread>("/threads", {
      method: "POST",
      body: { bot_ids: botIds, title },
      ...opts,
    }),

  remove: (threadId: string, opts?: Opts): Promise<void> =>
    request<void>(`/threads/${threadId}`, { method: "DELETE", ...opts }),

  messages: (threadId: string, opts?: Opts): Promise<Message[]> =>
    request<Message[]>(`/threads/${threadId}/messages`, { ...opts }),

  /** Non-streaming turn. Used directly, and as the fallback when SSE fails. */
  send: (threadId: string, content: string, opts?: Opts): Promise<unknown> =>
    request<unknown>(`/threads/${threadId}/messages`, {
      method: "POST",
      body: { content },
      // A full model turn can take a while; give it more room than a normal call.
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 120000,
    }),
}

/**
 * Finds the existing 1:1 thread with a bot, or creates one.
 *
 * Fixes the original bug where the chat screen created a brand new thread on every
 * mount. A thread counts as the bot's own chat when it targets exactly that bot.
 */
export async function findOrCreateBotThread(
  botId: string,
  title: string,
  opts?: Opts,
): Promise<{ thread: Thread; created: boolean }> {
  const existing = await threads.list(opts)
  const match = existing
    .filter((t) => t.bot_ids?.length === 1 && String(t.bot_ids[0]) === botId)
    .sort((a, b) => String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")))[0]
  if (match) return { thread: match, created: false }
  const thread = await threads.create([botId], title, opts)
  return { thread, created: true }
}

export interface ThreadStreamHandlers {
  onEvent: (event: TurnStreamEvent) => void
  /** Stream finished (cleanly or not). `fellBack` is true when SSE was unusable. */
  onClose: (info: { fellBack: boolean; error?: Error }) => void
}

/**
 * The one streaming interface the screens consume.
 *
 * Tries SSE over XHR. If the transport produced nothing usable -- no tokens, no
 * finished-turn `done`, no `error` -- it falls back to the plain `POST /messages` turn
 * and then reports `fellBack`, so callers get the same event shape either way and the
 * user's message is never lost.
 *
 * The close-out `done` (`{thread_id, reason:"closed"}`, emitted from the endpoint's
 * `finally` when a connection ends without a terminal event) deliberately does NOT
 * count as a real terminal event. Treating it as one would suppress the fallback and
 * leave the user staring at an empty bubble after a dropped connection.
 */
export function streamThreadMessage(
  threadId: string,
  content: string,
  handlers: ThreadStreamHandlers,
): EventStreamHandle {
  let sawTerminal = false
  let cancelled = false
  let fallbackActive = false

  const stream = openEventStream(`/threads/${threadId}/messages/stream`, {
    method: "POST",
    body: { content },
    onFrame: (frame) => {
      const event = parseSseEvent(frame.event, frame.data)
      if (event.event === "token" && event.data.delta.length > 0) sawTerminal = true
      if (event.event === "error") sawTerminal = true
      if (event.event === "done" && !isStreamClosedDone(event.data)) sawTerminal = true
      handlers.onEvent(event)
    },
    onClose: () => {
      if (cancelled) return
      if (!sawTerminal) {
        void runFallback()
        return
      }
      handlers.onClose({ fellBack: false })
    },
    onError: (error) => {
      if (cancelled) return
      if (!sawTerminal) {
        void runFallback(error)
        return
      }
      handlers.onEvent({ event: "error", data: { detail: error.message } })
      handlers.onClose({ fellBack: false, error })
    },
  })

  async function runFallback(streamError?: Error): Promise<void> {
    if (fallbackActive || cancelled) return
    fallbackActive = true
    try {
      await threads.send(threadId, content)
      if (cancelled) return
      handlers.onClose({ fellBack: true })
    } catch (error) {
      if (cancelled) return
      const err = error instanceof Error ? error : (streamError ?? new Error("Send failed."))
      handlers.onEvent({ event: "error", data: { detail: err.message } })
      handlers.onClose({ fellBack: true, error: err })
    }
  }

  return {
    get isClosed(): boolean {
      return cancelled || stream.isClosed
    },
    close(): void {
      cancelled = true
      stream.close()
    },
  }
}

/**
 * Passive subscription to turns pushed onto a thread by the worker or a routine.
 *
 * Per docs/API.md this stream carries NO `token` deltas: a turn opens with
 * `turn_started` and closes with a `done` that includes the full message `content`.
 * That makes it the right channel for a turn the phone did not initiate, and the right
 * fallback target when incremental streaming is unreliable -- one event, whole message.
 */
export function subscribeThreadEvents(
  threadId: string,
  onEvent: (event: ThreadEvent) => void,
  onError?: (error: Error) => void,
): EventStreamHandle {
  return openEventStream(`/threads/${threadId}/events`, {
    method: "GET",
    // The channel is idle most of the time, so allow a long silence before giving up.
    firstByteTimeoutMs: 120000,
    onFrame: (frame) => {
      // parseThreadEvent never returns null and never throws: an event name this build
      // does not know arrives on the `unknown` arm instead of breaking the client.
      onEvent(parseThreadEvent(frame.event, frame.data))
    },
    onError,
  })
}

/* ---------------------------------------------------------------- approvals */

export const approvals = {
  list: (params?: { status?: ApprovalStatus; bot_id?: string }, opts?: Opts): Promise<Approval[]> =>
    request<Approval[]>("/approvals", {
      query: { status: params?.status ?? "pending", bot_id: params?.bot_id },
      ...opts,
    }),

  get: (id: string, opts?: Opts): Promise<Approval> => request<Approval>(`/approvals/${id}`, { ...opts }),

  /** On `approved` the API executes the held action and returns `execution`. */
  decide: (
    id: string,
    decision: "approved" | "rejected",
    note?: string,
    opts?: Opts,
  ): Promise<ApprovalDecisionResult> =>
    request<ApprovalDecisionResult>(`/approvals/${id}/decide`, {
      method: "POST",
      body: note && note.trim() ? { decision, note: note.trim() } : { decision },
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 90000,
    }),
}

/* --------------------------------------------------------------------- runs */

/**
 * Runs — and specifically the two states where a run is waiting on a person.
 *
 * A run parked in `awaiting_human` is the whole reason this app has a takeover
 * screen: the agent drove until it hit a login it cannot pass and stopped. There
 * is no push for that (the API pushes approvals only), so the phone finds parked
 * runs by asking, which is what `parked()` is for.
 */
export const runs = {
  list: (
    params?: { status?: RunStatus | string; bot_id?: string; thread_id?: string; limit?: number },
    opts?: Opts,
  ): Promise<Run[]> =>
    request<Run[]>("/runs", {
      query: {
        status: params?.status,
        bot_id: params?.bot_id,
        thread_id: params?.thread_id,
        limit: params?.limit,
      },
      ...opts,
    }),

  get: (runId: string, opts?: Opts): Promise<Run> => request<Run>(`/runs/${runId}`, { ...opts }),

  /** Every run currently handed to a human. Newest first, as the API orders it. */
  parked: (opts?: Opts): Promise<Run[]> => runs.list({ status: "awaiting_human", limit: 50 }, opts),

  /**
   * "I've finished, continue" — resume the same task on the same screen.
   *
   * Two things callers must not get wrong:
   *
   * 1. `resumed: false` is **not an error.** The API claims the run with one
   *    conditional `awaiting_human -> running` update, so a double-press loses
   *    the race and is told so instead of starting a second agent loop against
   *    the browser session the person just authenticated. Show "already going".
   * 2. The resumed loop runs **synchronously inside this request** — it rebuilds
   *    the conversation, takes a fresh screenshot and carries on — so it can
   *    take minutes. The default 20s timeout would abort a resume that is
   *    working perfectly, and the retry that followed would be the double-press
   *    the idempotency is defending against.
   */
  resume: (runId: string, note?: string, opts?: Opts): Promise<ResumeRunOut> =>
    request<ResumeRunOut>(`/runs/${runId}/resume`, {
      method: "POST",
      body: note && note.trim() ? { note: note.trim() } : {},
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 180000,
    }),
}

/* --------------------------------------------------------------- work items */

/**
 * Work items — the owned, transferable unit of work a bot hands to another bot.
 *
 * Read-mostly on the phone. The one write kept is `transfer`, because handing a
 * stalled item to a different bot is a decision, and `reason` is required — a
 * ledger of timestamps without reasons is exactly what the competitor already
 * has. Creating and editing items are authoring jobs and live on the desktop.
 */
export const workItems = {
  list: (
    params?: { type?: string; status?: WorkItemStatus; owner_bot_id?: string; limit?: number },
    opts?: Opts,
  ): Promise<WorkItem[]> =>
    request<WorkItem[]>("/work-items", {
      query: {
        type: params?.type,
        status: params?.status,
        owner_bot_id: params?.owner_bot_id,
        limit: params?.limit,
      },
      ...opts,
    }),

  get: (id: string, opts?: Opts): Promise<WorkItem> => request<WorkItem>(`/work-items/${id}`, { ...opts }),

  /** The handover ledger, newest first. Every row is a real handover. */
  transfers: (id: string, opts?: Opts): Promise<WorkItemTransfer[]> =>
    request<WorkItemTransfer[]>(`/work-items/${id}/transfers`, { ...opts }),

  /**
   * Hand the item to another bot. `reason` is required by the API
   * (`min_length=1`) and is not padded here — an empty box must fail loudly in
   * the UI rather than be filled in with a placeholder nobody chose.
   *
   * Idempotent: transferring to the bot that already holds it answers
   * `transferred: false` and writes no second ledger row.
   */
  transfer: (id: string, toBotId: string, reason: string, opts?: Opts): Promise<WorkItemTransferResult> =>
    request<WorkItemTransferResult>(`/work-items/${id}/transfer`, {
      method: "POST",
      body: { to_bot_id: toBotId, reason },
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 30000,
    }),
}

/* ------------------------------------------------------------------ desktop */

export const desktop = {
  get: (botId: string, opts?: Opts): Promise<BotDesktop> => request<BotDesktop>(`/bots/${botId}/desktop`, { ...opts }),

  start: (botId: string, opts?: Opts): Promise<BotDesktop> =>
    request<BotDesktop>(`/bots/${botId}/desktop/start`, {
      method: "POST",
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 90000,
    }),

  stop: (botId: string, wipe = false, opts?: Opts): Promise<BotDesktop> =>
    request<BotDesktop>(`/bots/${botId}/desktop/stop`, {
      method: "POST",
      query: { wipe },
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 60000,
    }),

  suspend: (botId: string, opts?: Opts): Promise<BotDesktop> =>
    request<BotDesktop>(`/bots/${botId}/desktop/suspend`, { method: "POST", ...opts }),

  resume: (botId: string, opts?: Opts): Promise<BotDesktop> =>
    request<BotDesktop>(`/bots/${botId}/desktop/resume`, { method: "POST", ...opts }),

  /** Risk-gated: send/spend/delete actions come back as a pending approval. */
  action: (botId: string, input: DesktopActionInput, opts?: Opts): Promise<DesktopActionOutcome> =>
    request<DesktopActionOutcome>(`/bots/${botId}/desktop/action`, {
      method: "POST",
      body: input,
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 30000,
    }),

  screenshot: (botId: string, opts?: Opts): Promise<DesktopScreenshot> =>
    request<DesktopScreenshot>(`/bots/${botId}/desktop/screenshot`, {
      ...opts,
      retries: 0,
      timeoutMs: opts?.timeoutMs ?? 15000,
    }),

  windows: (botId: string, opts?: Opts): Promise<DesktopWindowsResult> =>
    request<DesktopWindowsResult>(`/bots/${botId}/desktop/windows`, { ...opts }),

  /**
   * Mint a short-lived capability to watch this desktop through the API proxy.
   *
   * This is the **only** way a phone can see a Bot Desktop. `BotDesktop.stream_url`
   * is a `10.60.x.x` address inside the Azure VNet with no public route — that
   * per-bot isolation is the product's headline claim, so it is not going to grow
   * one, and a WebView pointed at it fails every time.
   *
   * `retries: 0` on purpose: the ticket has a 60-second TTL and its control leg
   * is single-use, so a silent retry can mint one ticket, half-connect, and then
   * present a burned one. A failure here should surface.
   */
  streamTicket: (botId: string, opts?: Opts): Promise<DesktopStreamTicket> =>
    request<DesktopStreamTicket>(`/bots/${botId}/desktop/stream/ticket`, {
      method: "POST",
      ...opts,
      retries: 0,
      timeoutMs: opts?.timeoutMs ?? 15000,
    }),
}

/* -------------------------------------------------------------------- usage */

export const usage = {
  list: (days = 1, opts?: Opts): Promise<UsageOut[]> => request<UsageOut[]>("/usage", { query: { days }, ...opts }),
}

/* ------------------------------------------------------------------ devices */

export const devices = {
  /**
   * Registers this device's Expo push token so approvals can be pushed here.
   * `POST /me/devices` answers 201.
   */
  register: (registration: DeviceRegistration, opts?: Opts): Promise<unknown> =>
    request<unknown>("/me/devices", { method: "POST", body: registration, ...opts }),
}

export const api = {
  approvals,
  auth,
  bots,
  desktop,
  devices,
  health,
  runs,
  threads,
  usage,
  workItems,
  findOrCreateBotThread,
  streamThreadMessage,
  subscribeThreadEvents,
}
