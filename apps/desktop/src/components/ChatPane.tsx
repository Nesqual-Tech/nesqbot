import { Fragment, useCallback, useEffect, useMemo, useRef, type CSSProperties } from "react"
import { botColors, logoInk } from "@nesqbot/ui"
import { useMessages } from "../hooks/useMessages"
import { useThreadEvents } from "../hooks/useThreadEvents"
import { useThreads } from "../hooks/useThreads"
import { initials, truncate } from "../lib/format"
import { isTakeoverRequested, requestFromEvent } from "../lib/takeover"
import { useSelection, useToast } from "../state/AppState"
import { useTakeover } from "../state/takeover"
import { AgentActivity } from "./AgentActivity"
import { Composer } from "./Composer"
import { EmptyState, ErrorState } from "./EmptyState"
import { HandoffRail, MessageBubble } from "./MessageBubble"
import { SkeletonList } from "./Skeleton"
import type { Bot, Message } from "../types"

export interface ChatPaneProps {
  bots: Bot[]
  botsLoading: boolean
  botsError: unknown
  onApprovalRaised: (approvalId: string, title: string) => void
  onTurnComplete?: () => void
}

function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches
}

export function ChatPane({ bots, botsLoading, botsError, onApprovalRaised, onTurnComplete }: ChatPaneProps) {
  const toast = useToast()
  const takeover = useTakeover()
  const { activeBotId, activeThreadId, setActiveThreadId } = useSelection()
  const threads = useThreads()
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const ensuringRef = useRef<string | null>(null)

  const activeBot = useMemo(() => bots.find((b) => b.id === activeBotId) ?? null, [bots, activeBotId])
  const botById = useMemo(() => {
    const map: Record<string, Bot> = {}
    for (const bot of bots) map[bot.id] = bot
    return map
  }, [bots])

  /*
   * The handoff wiring.
   *
   * Both callbacks hand straight to the app-wide takeover state rather than
   * holding anything locally. A parked run outlives this component — the person
   * can switch teammates or close the app and still has to be able to answer
   * it — so the pane is a *source* of takeovers, never their owner.
   *
   * The `thread_id` fallback matters: the event carries one, but a stream that
   * is already scoped to this thread is the more reliable answer if it does not.
   */
  const messages = useMessages(activeThreadId, {
    onApproval: (data) => onApprovalRaised(data.approval_id, data.title),
    onDone: onTurnComplete,
    onTakeover: (data) => {
      if (isTakeoverRequested(data)) takeover.raise(requestFromEvent(data, activeThreadId))
      else takeover.clear(data.run_id)
    },
    onRunSettled: (runId, awaitingHuman) => {
      // A run that just parked is not in the poll's last answer yet; one that
      // finished should stop being offered as resumable immediately.
      if (awaitingHuman) void takeover.refresh()
      else takeover.clear(runId)
    },
  })

  // Worker/routine/mobile turns land here while the user is idle. This channel
  // carries no token deltas — `turn_started` raises the indicator below and
  // `done` brings the finished message with it.
  const events = useThreadEvents(activeThreadId, {
    enabled: Boolean(activeThreadId) && !messages.streaming,
    onEvent: messages.applyRemoteEvent,
  })

  const threadsForBot = useMemo(
    () => (activeBotId ? threads.threads.filter((t) => t.bot_ids?.includes(activeBotId)) : []),
    [threads.threads, activeBotId],
  )

  const activeThread = useMemo(
    () => threads.threads.find((t) => t.id === activeThreadId) ?? null,
    [threads.threads, activeThreadId],
  )

  // Make sure the selected bot always has a thread open.
  useEffect(() => {
    if (!activeBot || threads.initialising) return
    if (activeThread && activeThread.bot_ids?.includes(activeBot.id)) return
    if (ensuringRef.current === activeBot.id) return
    ensuringRef.current = activeBot.id
    void threads
      .ensureThreadForBot(activeBot)
      .then((thread) => setActiveThreadId(thread.id))
      .catch((err: unknown) => {
        toast.error("Could not open a thread", err instanceof Error ? err.message : undefined)
      })
      .finally(() => {
        ensuringRef.current = null
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeBot?.id, activeThread?.id, threads.initialising, threads.threads.length])

  // Stick to the bottom as tokens arrive.
  useEffect(() => {
    const node = scrollRef.current
    if (!node) return
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight
    if (distance > 240) return
    node.scrollTo({ top: node.scrollHeight, behavior: prefersReducedMotion() ? "auto" : "smooth" })
  }, [messages.messages.length, messages.streamText, messages.activity.length, messages.turn?.status])

  const send = useCallback(
    async (text: string) => {
      const outcome = await messages.send(text)
      if (!outcome.ok && outcome.error) toast.error("Message not delivered", outcome.error)
      if (outcome.fellBack) toast.warning("Streaming unavailable", "Sent the turn without live tokens.")
      return outcome
    },
    [messages, toast],
  )

  const newThread = useCallback(async () => {
    if (!activeBot) return
    try {
      const thread = await threads.createThread({
        bot_ids: [activeBot.id],
        title: `${activeBot.name} · ${new Date().toLocaleDateString()}`,
      })
      setActiveThreadId(thread.id)
    } catch (err) {
      toast.error("Could not create thread", err instanceof Error ? err.message : undefined)
    }
  }, [activeBot, threads, setActiveThreadId, toast])

  const removeThread = useCallback(async () => {
    if (!activeThreadId) return
    try {
      await threads.deleteThread(activeThreadId)
      setActiveThreadId(null)
      toast.success("Thread deleted")
    } catch (err) {
      toast.error("Could not delete thread", err instanceof Error ? err.message : undefined)
    }
  }, [activeThreadId, threads, setActiveThreadId, toast])

  if (botsError && bots.length === 0) {
    return (
      <div className="chat">
        <ErrorState
          error={botsError}
          title="Nesq Bot API unreachable"
          onRetry={() => {
            void threads.refetch()
          }}
        />
      </div>
    )
  }

  if (!activeBot) {
    return (
      <div className="chat">
        <div className="chat__header">
          <div className="chat__identity">
            <h2 className="chat__title">Chat</h2>
            <div className="chat__subtitle">No teammate selected</div>
          </div>
        </div>
        {botsLoading ? (
          <div className="chat__messages">
            <SkeletonList rows={4} />
          </div>
        ) : (
          <EmptyState
            glyph="chat"
            watermark
            title="Pick a teammate"
            description="Choose someone from the left. Work keeps running in Azure even when this app is closed."
          />
        )}
      </div>
    )
  }

  const streamingMessage: Message | null =
    messages.streamText && activeThreadId
      ? {
          id: "streaming",
          thread_id: activeThreadId,
          role: "assistant",
          content: messages.streamText,
          bot_id: messages.streamBotId ?? activeBot.id,
          created_at: new Date().toISOString(),
        }
      : null

  return (
    <div className="chat">
      <div className="chat__header">
        <div className="chat__identity">
          <h2 className="chat__title">
            <span
              className="avatar avatar--xs"
              style={{ "--avatar-bg": botColors[activeBot.slug] || logoInk } as CSSProperties}
              aria-hidden="true"
            >
              {initials(activeBot.name)}
            </span>
            {activeBot.name}
          </h2>
          <div className="chat__subtitle">
            {activeThread ? truncate(activeThread.title, 48) : "Opening thread…"}
            {events.connected ? (
              <span className="live-dot" title="Live thread subscription active">
                <span className="live-dot__pip" aria-hidden="true" /> live
              </span>
            ) : events.retrying ? (
              <span className="live-dot live-dot--warn" title={events.error ?? "Reconnecting"}>
                <span className="live-dot__pip" aria-hidden="true" /> reconnecting
              </span>
            ) : null}
          </div>
        </div>

        <div className="chat__header-actions">
          {threadsForBot.length > 1 ? (
            <>
              <label className="sr-only" htmlFor="thread-picker">
                Choose thread
              </label>
              <select
                id="thread-picker"
                className="select"
                value={activeThreadId ?? ""}
                onChange={(event) => setActiveThreadId(event.target.value || null)}
              >
                {threadsForBot.map((thread) => (
                  <option key={thread.id} value={thread.id}>
                    {truncate(thread.title, 40)}
                  </option>
                ))}
              </select>
            </>
          ) : null}
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => void newThread()}>
            New thread
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void removeThread()}
            disabled={!activeThreadId}
          >
            Delete
          </button>
        </div>
      </div>

      <div className="chat__messages" ref={scrollRef} aria-busy={messages.loading}>
        {messages.initialising ? <SkeletonList rows={3} /> : null}

        {messages.error && messages.messages.length === 0 && !messages.initialising ? (
          <ErrorState error={messages.error} title="Transcript unavailable" onRetry={() => void messages.refetch()} />
        ) : null}

        {!messages.initialising && !messages.error && messages.messages.length === 0 && !streamingMessage ? (
          <EmptyState
            glyph="spark"
            watermark
            title={`Say hello to ${activeBot.name}`}
            description={activeBot.role || "Ask for a status, a draft, or hand over a task."}
          />
        ) : null}

        {messages.messages.map((message) => {
          /*
           * A handover is an event between two messages, so it is rendered
           * between them rather than inside either. `meta.handoff_to` is
           * written by the orchestrator and was previously read by nothing —
           * see `HandoffRail` for why that mattered.
           */
          const handoffTo = typeof message.meta?.handoff_to === "string" ? message.meta.handoff_to : null
          const ledgerKey = typeof message.meta?.ledger_key === "string" ? message.meta.ledger_key : undefined
          const fromBot = message.bot_id ? botById[message.bot_id] : undefined
          const toBot = handoffTo ? botById[handoffTo] : undefined

          return (
            <Fragment key={message.id}>
              <MessageBubble message={message} bot={fromBot} />
              {toBot ? <HandoffRail from={fromBot} to={toBot} ledgerKey={ledgerKey} /> : null}
            </Fragment>
          )
        })}

        {streamingMessage ? (
          <MessageBubble
            message={streamingMessage}
            bot={streamingMessage.bot_id ? botById[streamingMessage.bot_id] : undefined}
            streaming
          />
        ) : null}

        {/*
          One object, not three. The spinner, the second spinner for a remote
          turn and the flat activity list all used to sit here separately; the
          rail folds them into a single account of what the agent is doing.
        */}
        <AgentActivity
          turn={messages.turn}
          steps={messages.activity}
          streaming={messages.streaming}
          botName={messages.remoteTurn?.botName ?? activeBot.name}
        />
      </div>

      <Composer
        placeholder={`Message ${activeBot.name}…`}
        disabled={!activeThreadId}
        streaming={messages.streaming}
        focusKey={activeThreadId}
        onSend={send}
        onStop={messages.stop}
      />
    </div>
  )
}
