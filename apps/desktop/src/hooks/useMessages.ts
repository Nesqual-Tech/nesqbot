/**
 * Thread transcript + the streaming send path.
 *
 * `send()` pushes an optimistic user bubble, opens
 * `POST /threads/{id}/messages/stream` and renders `token` deltas as they land.
 * If the stream never produces a single event it silently falls back to the
 * non-streaming turn; if it dies *after* work started it surfaces the error
 * instead of re-sending (the side effects may already have happened).
 */
import { useCallback, useEffect, useRef, useState } from "react"
import { doneEventText, isStreamClosedDone } from "@nesqbot/protocol"
import * as api from "../api/endpoints"
import { errorMessage } from "../api/client"
import type { SseHandle } from "../api/sse"
import { uid, usd } from "../lib/format"
import { isTakeoverRequested, parseTakeoverEvent, readDoneRun } from "../lib/takeover"
import { useAsyncResource } from "./useAsync"
import type { AttachmentUpload } from "@nesqbot/protocol"
import type {
  ApprovalEventData,
  ChannelEvent,
  DoneEventData,
  Message,
  TakeoverEventData,
  ThreadEvent,
  TurnStreamEvent,
} from "../types"

export type ActivityKind = "handoff" | "tool" | "approval" | "takeover" | "desktop" | "cost" | "error" | "info"

/** The structured half of a `tool` frame, kept so the UI is not parsing `text`. */
export interface ToolCall {
  connector: string
  action: string
  ok: boolean
}

/**
 * The structured half of a `desktop` frame.
 *
 * `starting` repeats with a rising `elapsedSeconds` — the API documents it as a
 * progress feed, not a one-shot — which is why these rows are keyed and
 * replaced rather than appended (see `pushActivity`). A cold start on ACI is
 * 30–90 seconds and can reach three minutes behind an image pull, and three
 * minutes of an unchanging rail is the "five minutes to open LinkedIn"
 * complaint written in a different product.
 */
export interface DesktopProgress {
  phase: "starting" | "ready" | "unavailable" | "blocked" | "finished"
  elapsedSeconds?: number
  detail?: string
  /** `finished` only: how many desktop actions actually ran. */
  steps?: number
}

/** The structured half of a `cost` frame — one step of an agent loop. */
export interface CostStep {
  step: number
  costUsd: number
  turnCostUsd: number
  spentTodayUsd: number
  budgetUsd: number
  imageTokens: number
}

export interface StreamActivity {
  id: string
  kind: ActivityKind
  text: string
  at: number
  approvalId?: string
  /**
   * Rows carrying the same key replace one another instead of stacking.
   *
   * Only `desktop:starting` uses this today, and it is the whole reason the
   * feed is legible: without it a ninety-second boot writes forty-five
   * near-identical rows and pushes everything the agent actually did off the
   * top of a twenty-row buffer.
   */
  key?: string
  /**
   * Present on `tool` steps only.
   *
   * `text` is still written, because a screen reader wants one sentence. But
   * the progress rail renders the connector, the action and the outcome as
   * three separate things with three different type roles, and re-splitting a
   * string it had just joined would be silly.
   */
  tool?: ToolCall
  /** Present on `desktop` steps only. */
  desktop?: DesktopProgress
  /** Present on `cost` steps only. */
  cost?: CostStep
  /**
   * The delegation path, e.g. `person → lead_generator → sales`. Set on a
   * `handoff` row only when the server said this was a delegation rather than
   * routing — the two are different events wearing one name.
   */
  chain?: string
}

export type TurnStatus = "running" | "done" | "failed" | "parked"

/**
 * The turn currently on screen, or the one that just finished.
 *
 * This is the difference between a spinner and watching somebody work, and all
 * of it is real data off the wire. `startedAt` is when the turn's first frame
 * arrived, `tier` and `costUsd` come off the `done` event, and `status` is
 * settled by a terminal frame rather than guessed from a timeout.
 *
 * There is deliberately no progress percentage. The agent does not know how
 * many steps a task will take, so the UI does not claim to either — it counts
 * what has actually happened and times it.
 */
export interface TurnState {
  status: TurnStatus
  startedAt: number
  /** Null while running. */
  endedAt: number | null
  /** Who is driving. Follows `handoff`. */
  botName: string | null
  /** Model tier the turn actually used. Null on a turn that called no model. */
  tier: string | null
  costUsd: number | null
  /** True when the turn was started elsewhere (worker, routine, mobile). */
  remote: boolean
}

/**
 * A file going out with a message, plus the preview the optimistic bubble
 * shows until the transcript refetches and the real attachment metadata
 * (with a server-side id to fetch bytes by) replaces it.
 */
export interface SendAttachment {
  upload: AttachmentUpload
  size: number
  previewUrl: string | null
}

export interface SendOutcome {
  ok: boolean
  error?: string
  fellBack?: boolean
  stopped?: boolean
}

export interface MessagesOptions {
  onApproval?: (data: ApprovalEventData) => void
  onDone?: () => void
  onActivity?: (activity: StreamActivity) => void
  /**
   * The agent hit something only a person can do. Fires on both channels — a
   * turn this client started can park just as easily as a worker-driven one.
   */
  onTakeover?: (data: TakeoverEventData) => void
  /**
   * A `done` frame naming a run. `awaitingHuman` is the API's new field: true
   * means the turn ended by parking on a human, false means that run is over
   * and any card holding it should go.
   */
  onRunSettled?: (runId: string, awaitingHuman: boolean) => void
}

export interface RemoteTurn {
  botId: string
  botName: string
  startedAt: number
}

export interface MessagesApi {
  messages: Message[]
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  /** True while *this* client's POST turn is streaming tokens. */
  streaming: boolean
  streamText: string
  streamBotId: string | null
  /** Set while a worker/routine/mobile turn is running on this thread. */
  remoteTurn: RemoteTurn | null
  activity: StreamActivity[]
  /**
   * The live turn, or the one that just ended. Null before anything has run on
   * this thread. Drives the progress rail in `AgentActivity`.
   */
  turn: TurnState | null
  clearActivity: () => void
  send: (content: string, mentionBotIds?: string[], attachments?: SendAttachment[]) => Promise<SendOutcome>
  stop: () => void
  /**
   * Feed an event from the passive `/threads/{id}/events` subscription.
   * That channel carries no `token` events, so this never touches the token
   * buffer: `turn_started` raises a typing indicator and `done` appends the
   * finished message it carries.
   */
  applyRemoteEvent: (event: ThreadEvent) => void
}

/**
 * A `done` frame that says something a transcript refetch will not: the turn
 * stopped without calling a model. `tier` is null exactly then.
 */
function budgetNote(data: DoneEventData): string | null {
  if (data.budget_blocked) return "Daily budget reached — the turn stopped before calling a model."
  if (data.tier === null) return "Turn finished without calling a model."
  return null
}

export function useMessages(threadId: string | null, options: MessagesOptions = {}): MessagesApi {
  const resource = useAsyncResource<Message[]>(
    (signal) => (threadId ? api.listMessages(threadId, signal) : Promise.resolve([])),
    [threadId],
    { initialData: [], enabled: Boolean(threadId) },
  )
  const { setData, refetch } = resource

  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState("")
  const [streamBotId, setStreamBotId] = useState<string | null>(null)
  const [remoteTurn, setRemoteTurn] = useState<RemoteTurn | null>(null)
  const [activity, setActivity] = useState<StreamActivity[]>([])
  const [turn, setTurn] = useState<TurnState | null>(null)

  const handleRef = useRef<SseHandle | null>(null)
  const stoppedRef = useRef(false)
  const optionsRef = useRef(options)
  optionsRef.current = options

  // Switching threads (or unmounting) must not leave a turn streaming.
  useEffect(() => {
    setStreaming(false)
    setStreamText("")
    setStreamBotId(null)
    setRemoteTurn(null)
    setActivity([])
    setTurn(null)
    return () => {
      handleRef.current?.close()
      handleRef.current = null
    }
  }, [threadId])

  type ActivityExtra = Pick<StreamActivity, "approvalId" | "tool" | "desktop" | "cost" | "chain" | "key">

  const pushActivity = useCallback((kind: ActivityKind, text: string, extra?: ActivityExtra) => {
    const item: StreamActivity = { id: uid("act"), kind, text, at: Date.now(), ...extra }
    setActivity((prev) => {
      /*
       * A keyed row updates in place. The row keeps its original `id` so React
       * does not remount it — a boot-progress row that remounted every two
       * seconds would replay its entrance animation forty-five times — and
       * keeps its original `at`, so the elapsed gap the rail prints beside it
       * stays the gap since the boot *started* rather than since the last
       * heartbeat.
       */
      if (extra?.key) {
        const index = prev.findIndex((row) => row.key === extra.key)
        if (index !== -1) {
          const next = [...prev]
          next[index] = { ...item, id: prev[index].id, at: prev[index].at }
          return next
        }
      }
      return [...prev.slice(-19), item]
    })
    optionsRef.current.onActivity?.(item)
  }, [])

  /**
   * Start (or restart) the turn clock.
   *
   * Called from exactly two places, because there are exactly two ways a turn
   * begins: this window sent it, or the passive channel says somebody else
   * did. Both reset the whole record — a second turn must not inherit the
   * previous one's cost.
   */
  const beginTurn = useCallback((botName: string | null, remote: boolean) => {
    setTurn({
      status: "running",
      startedAt: Date.now(),
      endedAt: null,
      botName,
      tier: null,
      costUsd: null,
      remote,
    })
  }, [])

  /** Settle the turn. A turn already settled stays settled. */
  const settleTurn = useCallback((status: Exclude<TurnStatus, "running">, patch?: Partial<TurnState>) => {
    setTurn((prev) => (prev ? { ...prev, status, endedAt: prev.endedAt ?? Date.now(), ...patch } : prev))
  }, [])

  const clearActivity = useCallback(() => setActivity([]), [])

  /**
   * A `done` frame's run bookkeeping. Both channels get it, and it is the
   * signal that closes a takeover card the person never had to answer —
   * the agent got past the wall on its own, or the run failed.
   */
  const applyDoneRun = useCallback((data: DoneEventData) => {
    const { runId, awaitingHuman } = readDoneRun(data)
    if (runId) optionsRef.current.onRunSettled?.(runId, awaitingHuman)
  }, [])

  /** Shared side-channel events — identical on both streams. */
  const applySideEvent = useCallback(
    (event: ChannelEvent): boolean => {
      /*
       * `takeover` first, because it arrives on the `unknown` arm: this build's
       * `@nesqbot/protocol` has no case for it, and that arm is precisely the
       * forward-compatibility hatch it was given for a server event added
       * later. Handled here rather than in the two channel switches so there is
       * exactly one place that knows the frame's shape.
       */
      const handoff = parseTakeoverEvent(event)
      if (handoff) {
        if (isTakeoverRequested(handoff)) {
          pushActivity("takeover", handoff.reason || "Waiting for you to finish a step")
          // Parked, not finished and not broken. The rail says so rather than
          // leaving a spinner running against a turn that has stopped.
          settleTurn("parked")
        }
        optionsRef.current.onTakeover?.(handoff)
        return true
      }

      switch (event.event) {
        case "handoff": {
          /*
           * One event name, two mechanisms — see "Bots working together" in
           * `docs/API.md`. **Routing** picks which bot answers one message and
           * hands over nothing; **delegation** is a bot giving work to another
           * bot through `delegate_to_bot`, and it sets `from_bot_name`,
           * `chain`, `run_id` and `delegated`. Those five keys were on the wire
           * and read by nothing here, so a team visibly working looked
           * identical to a topic change.
           */
          const { bot_name: to, from_bot_name: from, delegated, chain } = event.data
          const text =
            delegated && from
              ? `${from} delegated to ${to || "another teammate"}`
              : `Handed off to ${to || "another teammate"}`
          pushActivity("handoff", text, { chain: delegated ? chain : undefined })
          if (to) setTurn((prev) => (prev ? { ...prev, botName: to } : prev))
          return true
        }
        case "tool":
          pushActivity(
            "tool",
            `${event.data.connector}.${event.data.action} ${event.data.ok ? "succeeded" : "failed"}`,
            { tool: { connector: event.data.connector, action: event.data.action, ok: event.data.ok } },
          )
          return true
        case "approval":
          pushActivity("approval", event.data.title || "Approval required", {
            approvalId: event.data.approval_id,
          })
          optionsRef.current.onApproval?.(event.data)
          return true

        /*
         * The frame that turns three minutes of silence into three minutes of
         * being told what is going on.
         *
         * The API emits this while a bot brings its desktop up, precisely
         * because a cold start on ACI takes 30–90 seconds and a client with no
         * `desktop` handling shows a turn that looks hung. This build had no
         * such handling: every one of these frames fell through to `default`
         * and was dropped on the floor.
         *
         * `starting` is keyed, so the repeats update one row instead of
         * flooding the rail.
         */
        case "desktop": {
          const { phase, detail, elapsed_seconds: elapsed, steps } = event.data
          const progress: DesktopProgress = {
            phase,
            elapsedSeconds: typeof elapsed === "number" ? elapsed : undefined,
            detail: detail ?? undefined,
            steps: typeof steps === "number" ? steps : undefined,
          }
          const text =
            phase === "starting"
              ? (detail ?? "Bringing the bot desktop up — a cold start takes 30–90 seconds")
              : phase === "ready"
                ? "Bot desktop ready"
                : phase === "unavailable"
                  ? `The bot desktop would not start${detail ? `: ${detail}` : ""}`
                  : phase === "blocked"
                    ? `Starting the desktop needs approval here${detail ? `: ${detail}` : ""}`
                    : typeof steps === "number"
                      ? `Desktop work finished — ${steps} ${steps === 1 ? "step" : "steps"}`
                      : "Desktop work finished"

          pushActivity("desktop", text, {
            desktop: progress,
            // Only the repeating phase is keyed. `ready` and `finished` are
            // events in their own right and must stay in the record.
            key: phase === "starting" ? "desktop:starting" : undefined,
          })
          return true
        }

        /*
         * What the turn has spent, while it is spending it.
         *
         * A single autonomous desktop loop can eat a day's budget, and the
         * failure this event exists to prevent is finding that out afterwards.
         * One row, keyed, so it counts up in place.
         */
        case "cost": {
          const d = event.data
          pushActivity("cost", `Spent ${usd(d.turn_cost_usd, true)} on this turn so far`, {
            key: "cost",
            cost: {
              step: d.step,
              costUsd: d.cost_usd,
              turnCostUsd: d.turn_cost_usd,
              spentTodayUsd: d.spent_today_usd,
              budgetUsd: d.budget_usd,
              imageTokens: d.image_tokens,
            },
          })
          return true
        }

        default:
          return false
      }
    },
    [pushActivity, settleTurn],
  )

  /**
   * Events from *our* POST turn stream: `token` deltas accumulate into the live
   * buffer and `done` flushes it.
   */
  const applyStreamEvent = useCallback(
    (event: TurnStreamEvent) => {
      switch (event.event) {
        case "token":
          setStreaming(true)
          setStreamText((prev) => prev + event.data.delta)
          break
        case "handoff":
          setStreamBotId(event.data.bot_id || null)
          applySideEvent(event)
          break
        case "done": {
          // Two frames share this name. A close-out means the connection ended
          // without a terminal event — nothing finished, so do not flush.
          if (isStreamClosedDone(event.data)) {
            setStreaming(false)
            break
          }
          setStreaming(false)
          setStreamText("")
          setStreamBotId(null)
          applyDoneRun(event.data)
          // The two numbers the rail exists to show. Both are optional on the
          // wire, and `tier: null` is meaningful (no model was called), so an
          // absent field and an explicit null are kept distinct.
          settleTurn(event.data.approval_id ? "parked" : "done", {
            tier: event.data.tier ?? null,
            costUsd: typeof event.data.cost_usd === "number" ? event.data.cost_usd : null,
            botName: event.data.bot_name ?? null,
          })
          const note = budgetNote(event.data)
          if (note) pushActivity("error", note)
          void refetch()
          optionsRef.current.onDone?.()
          break
        }
        case "error":
          setStreaming(false)
          settleTurn("failed")
          pushActivity("error", event.data.detail || "Stream error")
          break
        default:
          applySideEvent(event)
          break
      }
    },
    [applyDoneRun, applySideEvent, pushActivity, refetch, settleTurn],
  )

  /**
   * Events from the passive subscription. No tokens ever arrive here, so the
   * token buffer is left completely alone: `turn_started` only raises the
   * typing indicator and `done` appends the complete message it carries.
   */
  const applyRemoteEvent = useCallback(
    (event: ThreadEvent) => {
      switch (event.event) {
        case "turn_started":
          setRemoteTurn({
            botId: event.data.bot_id,
            botName: event.data.bot_name || "A teammate",
            startedAt: Date.now(),
          })
          beginTurn(event.data.bot_name || "A teammate", true)
          pushActivity("info", `${event.data.bot_name || "A teammate"} started a turn`)
          break
        case "handoff":
          setRemoteTurn((prev) =>
            prev
              ? { ...prev, botId: event.data.bot_id || prev.botId, botName: event.data.bot_name || prev.botName }
              : prev,
          )
          applySideEvent(event)
          break
        case "done": {
          setRemoteTurn(null)
          // Close-out frame: the subscription ended, no turn completed.
          if (isStreamClosedDone(event.data)) break

          applyDoneRun(event.data)
          settleTurn(event.data.approval_id ? "parked" : "done", {
            tier: event.data.tier ?? null,
            costUsd: typeof event.data.cost_usd === "number" ? event.data.cost_usd : null,
            botName: event.data.bot_name ?? null,
          })

          // The wire field is `message`; `doneEventText` reads either spelling
          // so this never depends on which one the server sends.
          const text = doneEventText(event.data)
          if (text) {
            const messageId = event.data.message_id ?? uid("remote")
            setData((prev) =>
              prev.some((m) => m.id === messageId)
                ? prev
                : [
                    ...prev,
                    {
                      id: messageId,
                      thread_id: threadId ?? "",
                      role: "assistant",
                      content: text,
                      bot_id: event.data.bot_id ?? null,
                      created_at: new Date().toISOString(),
                    },
                  ],
            )
          }
          const note = budgetNote(event.data)
          if (note) pushActivity("error", note)
          // Reconcile ids/ordering with the server transcript.
          void refetch()
          optionsRef.current.onDone?.()
          break
        }
        case "error":
          setRemoteTurn(null)
          settleTurn("failed")
          pushActivity("error", event.data.detail || "The background turn failed")
          break
        default:
          applySideEvent(event)
          break
      }
    },
    [applyDoneRun, applySideEvent, beginTurn, pushActivity, refetch, setData, settleTurn, threadId],
  )

  const stop = useCallback(() => {
    stoppedRef.current = true
    handleRef.current?.close()
    handleRef.current = null
    setStreaming(false)
  }, [])

  const send = useCallback(
    async (content: string, mentionBotIds?: string[], attachments?: SendAttachment[]): Promise<SendOutcome> => {
      const trimmed = content.trim()
      const files = attachments ?? []
      if (!threadId) return { ok: false, error: "Pick a teammate to start a thread." }
      if (!trimmed && files.length === 0) return { ok: false, error: "Nothing to send." }
      if (streaming) return { ok: false, error: "Still streaming — stop the current reply first." }

      const optimisticId = uid("local")
      const uploads = files.map((f) => f.upload)
      setData((prev) => [
        ...prev,
        {
          id: optimisticId,
          thread_id: threadId,
          role: "user",
          content: trimmed,
          created_at: new Date().toISOString(),
          // The same shape the API will list back, minus the bytes, so the
          // bubble renders identically before and after the refetch.
          meta: files.length
            ? {
                attachments: files.map((f) => ({ name: f.upload.name, media_type: f.upload.media_type, size: f.size })),
              }
            : undefined,
          ...(files.length ? { _previews: files.map((f) => f.previewUrl) } : {}),
        } as Message,
      ])

      stoppedRef.current = false
      setActivity([])
      setStreamText("")
      setStreamBotId(null)
      setStreaming(true)
      beginTurn(null, false)

      // A holder object keeps TypeScript's control-flow analysis honest about
      // values written from inside the stream callbacks. Named `frames` rather
      // than `turn` since the hook grew a `turn` state value — shadowing it
      // here would be a trap for the next reader.
      const frames: {
        received: boolean
        doneSeen: boolean
        closedEarly: boolean
        failure: string | null
      } = { received: false, doneSeen: false, closedEarly: false, failure: null }

      const closeReason = await new Promise<"done" | "aborted" | "error">((resolve) => {
        const handle = api.openMessageStream(
          threadId,
          { content: trimmed, mention_bot_ids: mentionBotIds, attachments: uploads.length ? uploads : undefined },
          {
            onEvent: (event) => {
              // A close-out `done` is the endpoint saying the connection ended,
              // not that the turn ran. It must NOT count as a received event:
              // a connection that drops before producing anything emits only
              // this, and treating it as work done would suppress the
              // non-streaming fallback and lose the user message entirely.
              if (event.event === "done" && isStreamClosedDone(event.data)) {
                frames.closedEarly = true
              } else {
                frames.received = true
                if (event.event === "done") frames.doneSeen = true
              }
              if (event.event === "error") frames.failure = event.data.detail
              applyStreamEvent(event)
            },
            onError: (err) => {
              frames.failure = errorMessage(err)
            },
            onClose: (reason) => resolve(reason),
          },
        )
        handleRef.current = handle
      })

      handleRef.current = null
      setStreaming(false)
      setStreamText("")
      setStreamBotId(null)

      if (stoppedRef.current || closeReason === "aborted") {
        settleTurn("done")
        await refetch()
        return { ok: true, stopped: true }
      }

      if (frames.doneSeen) {
        await refetch()
        return { ok: true }
      }

      // Nothing real ever arrived — the stream is unavailable, or the
      // connection dropped straight to a close-out. Either way no work
      // started, so the plain turn is safe to run.
      if (!frames.received) {
        try {
          await api.sendMessage(threadId, {
            content: trimmed,
            mention_bot_ids: mentionBotIds,
            attachments: uploads.length ? uploads : undefined,
          })
          settleTurn("done")
          await refetch()
          optionsRef.current.onDone?.()
          pushActivity("info", "Streaming unavailable — sent without live tokens.")
          return { ok: true, fellBack: true }
        } catch (err) {
          settleTurn("failed")
          setData((prev) => prev.filter((m) => m.id !== optimisticId))
          return { ok: false, error: errorMessage(err) }
        }
      }

      // Partially streamed then broke: never re-send, just report.
      settleTurn("failed")
      await refetch()
      return {
        ok: false,
        error:
          frames.failure ??
          (frames.closedEarly
            ? "The reply stream closed before the turn finished."
            : "The reply stream ended unexpectedly."),
      }
    },
    [threadId, streaming, setData, applyStreamEvent, refetch, pushActivity, beginTurn, settleTurn],
  )

  return {
    messages: resource.data,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch,
    streaming,
    streamText,
    streamBotId,
    remoteTurn,
    activity,
    turn,
    clearActivity,
    send,
    stop,
    applyRemoteEvent,
  }
}
