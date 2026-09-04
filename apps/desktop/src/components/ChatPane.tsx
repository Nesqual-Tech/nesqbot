import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useMessages } from "../hooks/useMessages"
import { useThreadEvents } from "../hooks/useThreadEvents"
import type { ThreadsApi } from "../hooks/useThreads"
import { cx, truncate } from "../lib/format"
import type { StagedAttachment } from "../lib/attachments"
import { isTakeoverRequested, requestFromEvent } from "../lib/takeover"
import { useSelection, useToast } from "../state/AppState"
import { useTakeover } from "../state/takeover"
import { AgentActivity } from "./AgentActivity"
import { BotAvatar, BotAvatarStack } from "./BotAvatar"
import { BotPersonaCard } from "./BotPersonaCard"
import { Composer } from "./Composer"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { HandoffRail, MessageBubble } from "./MessageBubble"
import { SkeletonList } from "./Skeleton"
import type { Bot, Message } from "../types"

export interface ChatPaneProps {
  bots: Bot[]
  botsLoading: boolean
  botsError: unknown
  /** Owned by the shell, because the conversation list draws from it too. */
  threads: ThreadsApi
  onApprovalRaised: (approvalId: string, title: string) => void
  onTurnComplete?: () => void
  desktopOpen: boolean
  onToggleDesktop: () => void
  /** Opens the settings sheet on the bot builder, for "Edit profile". */
  onEditProfile: (botId: string) => void
}

function prefersReducedMotion(): boolean {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches
}

/**
 * Three things worth asking this teammate, on an empty thread.
 *
 * Not decoration and not a tutorial: a blank composer next to a bot whose only
 * description is "Outbound research and drafts" is a guessing game about
 * register — do you type a keyword, or a sentence? Every one of these is
 * phrased as an instruction with a real object in it, because that is the
 * shape of prompt these bots answer well and the shape people write worst.
 *
 * Keyed by slug with a generic fallback, so a custom bot gets sensible ones
 * rather than none.
 */
const SUGGESTIONS: Record<string, string[]> = {
  chief_of_staff: [
    "Give Lead Generator and Sales one job each for today and tell me who has what.",
    "What is waiting on me right now, and what is waiting on a bot?",
    "Break this down and hand it out: we need ten qualified CRM leads by Friday.",
  ],
  lead_generator: [
    "Find ten companies hiring their first sales rep — name a person I can contact at each.",
    "Who at these accounts owns the CRM decision? Give me a name and a source.",
    "Draft an opener for the three best leads you found last time.",
  ],
  sales: [
    "Which deals have gone quiet for more than a week?",
    "Draft the follow-up for my last call and log what we agreed.",
    "Tidy the CRM: what is missing a next step?",
  ],
  ops: [
    "Triage the inbox and tell me only what needs me.",
    "File this month's invoices and flag anything unpaid.",
    "Run the onboarding checklist for the newest account.",
  ],
  support: [
    "What is in the ticket queue, worst first?",
    "Draft a reply to the oldest open ticket, quoting the KB article you used.",
    "Which questions keep coming back that the KB does not answer?",
  ],
}

const GENERIC_SUGGESTIONS = [
  "What can you do for me today?",
  "Take this and run with it:",
  "What do you need from me to get started?",
]

export function ChatPane({
  bots,
  botsLoading,
  botsError,
  threads,
  onApprovalRaised,
  onTurnComplete,
  desktopOpen,
  onToggleDesktop,
  onEditProfile,
}: ChatPaneProps) {
  const toast = useToast()
  const takeover = useTakeover()
  const { activeBotId, activeThreadId, setActiveThreadId } = useSelection()
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const ensuringRef = useRef<string | null>(null)
  const [personaOpen, setPersonaOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [prefill, setPrefill] = useState<{ text: string; key: number } | null>(null)

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
    async (text: string, mentionBotIds: string[], attachments: StagedAttachment[]) => {
      const outcome = await messages.send(
        text,
        mentionBotIds.length ? mentionBotIds : undefined,
        attachments.length
          ? attachments.map((a) => ({ upload: a.upload, size: a.size, previewUrl: a.previewUrl }))
          : undefined,
      )
      if (!outcome.ok && outcome.error) toast.error("Message not delivered", outcome.error)
      if (outcome.fellBack) toast.warning("Streaming unavailable", "Sent the turn without live tokens.")
      return outcome
    },
    [messages, toast],
  )

  /*
   * The roster is the feature, not a detail. `orchestrator._delegate_targets`
   * is "everyone else in this room", so a thread with one bot means
   * `delegate_to_bot` is never advertised and a chief of staff asked to hand
   * work over holds no tool that can. This app created every thread with one
   * bot and offered no way to add another, which is why no hand-off in the
   * shipped product ever reached a teammate.
   */
  const seatBot = useCallback(
    async (botId: string) => {
      if (!activeThreadId || !botId) return
      try {
        await threads.addBots(activeThreadId, [botId])
      } catch (err) {
        toast.error("Could not add that teammate", err instanceof Error ? err.message : undefined)
      }
    },
    [activeThreadId, threads, toast],
  )

  const unseatBot = useCallback(
    async (botId: string) => {
      if (!activeThreadId) return
      try {
        await threads.removeBot(activeThreadId, botId)
      } catch (err) {
        // The API refuses to empty a roster (409 last_bot_in_thread) because a
        // thread with no bots cannot answer. Say that rather than nothing.
        toast.error("Could not remove that teammate", err instanceof Error ? err.message : undefined)
      }
    },
    [activeThreadId, threads, toast],
  )

  const participants = useMemo(() => {
    // The API returns the roster ordered by id, which is arbitrary to a reader.
    // The bot whose window this is comes first - it is the one that answers -
    // and the rest are alphabetical so the strip does not reshuffle when
    // somebody is added.
    const seated = (activeThread?.bot_ids ?? []).map((id) => botById[id]).filter(Boolean)
    return seated.sort((a, b) => {
      if (a.id === activeBotId) return -1
      if (b.id === activeBotId) return 1
      return a.name.localeCompare(b.name)
    })
  }, [activeThread?.bot_ids, botById, activeBotId])

  const seatable = useMemo(
    () => bots.filter((b) => !(activeThread?.bot_ids ?? []).includes(b.id)),
    [bots, activeThread?.bot_ids],
  )

  const group = participants.length > 1

  /*
   * Who `@` offers, seated first.
   *
   * Not limited to the roster. Mentioning somebody who is not in the room is
   * how they get into it — `orchestrator._seat_mentioned_bots` reads the text
   * and seats them — so restricting the picker to `participants` would hide
   * the one thing the feature is for and leave "add a teammate" as the only
   * door. Seated names come first because they are the likelier target and
   * because the order should not change under you as people join.
   */
  const mentionCandidates = useMemo(
    () => [...participants, ...bots.filter((b) => !participants.some((p) => p.id === b.id))],
    [participants, bots],
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

  const commitRename = useCallback(async () => {
    const title = (renaming ?? "").trim()
    setRenaming(null)
    if (!activeThreadId || !title || title === activeThread?.title) return
    try {
      await threads.updateThread(activeThreadId, { title })
    } catch (err) {
      toast.error("Could not rename", err instanceof Error ? err.message : undefined)
    }
  }, [renaming, activeThreadId, activeThread?.title, threads, toast])

  const togglePinned = useCallback(async () => {
    if (!activeThreadId) return
    try {
      const next = await threads.updateThread(activeThreadId, { pinned: !activeThread?.pinned })
      toast.success(next.pinned ? "Pinned" : "Unpinned", next.pinned ? "It stays at the top of the list." : undefined)
    } catch (err) {
      toast.error("Could not update", err instanceof Error ? err.message : undefined)
    }
  }, [activeThreadId, activeThread?.pinned, threads, toast])

  /** What the person last sent here — arrow-up in the composer recalls it. */
  const lastSent = useMemo(() => {
    for (let i = messages.messages.length - 1; i >= 0; i -= 1) {
      const m = messages.messages[i]
      if (m.role === "user" && m.content.trim()) return m.content
    }
    return null
  }, [messages.messages])

  const removeThread = useCallback(async () => {
    if (!activeThreadId) return
    try {
      await threads.deleteThread(activeThreadId)
      setActiveThreadId(null)
      toast.success("Conversation deleted")
    } catch (err) {
      toast.error("Could not delete conversation", err instanceof Error ? err.message : undefined)
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
        {botsLoading ? (
          <div className="chat__messages">
            <SkeletonList rows={4} />
          </div>
        ) : (
          <EmptyState
            glyph="chat"
            watermark
            title="Pick a conversation"
            description="Choose one on the left, or press + to start a new one. Work keeps running in Azure even when this app is closed."
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

  const suggestions = SUGGESTIONS[activeBot.slug] ?? GENERIC_SUGGESTIONS
  const empty = !messages.initialising && !messages.error && messages.messages.length === 0 && !streamingMessage

  return (
    <div className="chat">
      <div className="chat__header">
        {/*
          The identity is the way into the profile, on the thing a person
          already looks at to see who they are talking to. Its prompt, its
          address, its connectors and its spend were all unreachable from this
          app until `GET /bots/{id}/persona` existed.
        */}
        <button
          type="button"
          className="chat__identity"
          onClick={() => setPersonaOpen(true)}
          aria-haspopup="dialog"
          title={group ? "Who is in this conversation?" : `Who is ${activeBot.name}?`}
        >
          {group ? <BotAvatarStack bots={participants} size={26} /> : <BotAvatar bot={activeBot} size={30} />}
          <span className="chat__identity-text">
            <span className="chat__title">
              {activeBot.name}
              {activeThread?.pinned ? (
                <span className="chat__pin" title="Pinned">
                  <Icon name="pin" size={11} />
                </span>
              ) : null}
            </span>
            <span className="chat__subtitle">
              {group
                ? participants.map((bot) => bot.name).join(" · ")
                : activeBot.role || truncate(activeThread?.title ?? "", 40)}
              {events.connected ? (
                <span className="live-dot" title="Live thread subscription active">
                  <span className="live-dot__pip" aria-hidden="true" /> live
                </span>
              ) : events.retrying ? (
                <span className="live-dot live-dot--warn" title={events.error ?? "Reconnecting"}>
                  <span className="live-dot__pip" aria-hidden="true" /> reconnecting
                </span>
              ) : null}
            </span>
          </span>
        </button>

        <div className="chat__header-actions">
          <button
            type="button"
            className="chat__menu-button"
            aria-expanded={menuOpen}
            aria-label="Conversation menu"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <Icon name="more" size={16} />
          </button>
          <button
            type="button"
            className={cx("btn btn--sm", desktopOpen ? "btn--primary" : "btn--ghost")}
            onClick={onToggleDesktop}
            aria-pressed={desktopOpen}
            title="The teammate's own Linux desktop (Ctrl ⇧ D)"
          >
            <Icon name="monitor" size={15} />
            Computer
          </button>
        </div>

        {menuOpen ? (
          <div className="chat__menu" role="menu" onMouseLeave={() => setMenuOpen(false)}>
            <div className="chat__menu-label">In this conversation</div>
            <ul className="chat__menu-roster">
              {participants.map((bot) => (
                <li key={bot.id}>
                  <BotAvatar bot={bot} size={18} />
                  <span>{bot.name}</span>
                  {participants.length > 1 ? (
                    <button
                      type="button"
                      className="chat__menu-remove"
                      onClick={() => void unseatBot(bot.id)}
                      aria-label={`Remove ${bot.name} from this conversation`}
                    >
                      <Icon name="close" size={12} />
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
            {seatable.length > 0 ? (
              <>
                <label className="sr-only" htmlFor="chat-add-teammate">
                  Add a teammate to this conversation
                </label>
                <select
                  id="chat-add-teammate"
                  className="select select--sm"
                  value=""
                  onChange={(event) => void seatBot(event.target.value)}
                >
                  <option value="">Add a teammate…</option>
                  {seatable.map((bot) => (
                    <option key={bot.id} value={bot.id}>
                      {bot.name}
                    </option>
                  ))}
                </select>
                <p className="chat__menu-note">
                  Anyone on your team can be handed work from here — the bot doing the handing seats them itself, and
                  their reply lands in this conversation. Seat somebody now to have them read along from the start.
                </p>
              </>
            ) : null}
            <div className="chat__menu-divider" />
            <button
              type="button"
              role="menuitem"
              className="chat__menu-item"
              disabled={!activeThreadId}
              onClick={() => {
                setMenuOpen(false)
                setRenaming(activeThread?.title ?? "")
              }}
            >
              Rename conversation
            </button>
            <button
              type="button"
              role="menuitem"
              className="chat__menu-item"
              disabled={!activeThreadId}
              onClick={() => {
                setMenuOpen(false)
                void togglePinned()
              }}
            >
              {activeThread?.pinned ? "Unpin" : "Pin to top"}
            </button>
            <button
              type="button"
              role="menuitem"
              className="chat__menu-item"
              onClick={() => {
                setMenuOpen(false)
                void newThread()
              }}
            >
              New thread
            </button>
            <button
              type="button"
              role="menuitem"
              className="chat__menu-item chat__menu-item--danger"
              disabled={!activeThreadId}
              onClick={() => {
                setMenuOpen(false)
                void removeThread()
              }}
            >
              Delete conversation
            </button>
          </div>
        ) : null}
      </div>

      {renaming !== null ? (
        <form
          className="chat__rename"
          onSubmit={(event) => {
            event.preventDefault()
            void commitRename()
          }}
        >
          <label className="sr-only" htmlFor="chat-rename">
            Conversation name
          </label>
          <input
            id="chat-rename"
            className="input"
            autoFocus
            maxLength={200}
            value={renaming}
            onChange={(event) => setRenaming(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setRenaming(null)
            }}
            onBlur={() => void commitRename()}
          />
          <button type="submit" className="btn btn--primary btn--sm">
            Save
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => setRenaming(null)}
          >
            Cancel
          </button>
        </form>
      ) : null}

      {personaOpen ? (
        <div className="modal" role="dialog" aria-modal="true">
          <button
            type="button"
            className="modal__backdrop"
            aria-label="Close profile"
            onClick={() => setPersonaOpen(false)}
          />
          <div className="modal__panel">
            <BotPersonaCard
              bot={activeBot}
              onClose={() => setPersonaOpen(false)}
              onOpenComputer={() => {
                setPersonaOpen(false)
                if (!desktopOpen) onToggleDesktop()
              }}
              onEditProfile={() => {
                setPersonaOpen(false)
                onEditProfile(activeBot.id)
              }}
            />
          </div>
        </div>
      ) : null}

      <div className="chat__messages" ref={scrollRef} aria-busy={messages.loading}>
        {messages.initialising ? <SkeletonList rows={3} /> : null}

        {messages.error && messages.messages.length === 0 && !messages.initialising ? (
          <ErrorState error={messages.error} title="Transcript unavailable" onRetry={() => void messages.refetch()} />
        ) : null}

        {empty ? (
          <div className="chat__opening">
            <EmptyState
              glyph="spark"
              watermark
              title={group ? "Start the group" : `Message ${activeBot.name}`}
              description={
                group
                  ? "Everybody seated here reads along from the start. Your teammates can be handed work whether or not they are in the room yet. Send, spend and delete still wait for you."
                  : "Say what you need. Their standing job is in the profile if you want a reminder."
              }
            />
            {!group ? (
              <div className="chat__suggestions" aria-label="Suggested messages">
                {suggestions.map((text) => (
                  <button
                    key={text}
                    type="button"
                    className="chat__suggestion"
                    onClick={() => setPrefill({ text, key: Date.now() })}
                  >
                    {text}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
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
        placeholder={group ? "Message the group…" : `Message ${activeBot.name}…`}
        disabled={!activeThreadId}
        streaming={messages.streaming}
        focusKey={activeThreadId}
        prefill={prefill}
        mentionCandidates={mentionCandidates}
        lastSent={lastSent}
        hint={
          group
            ? "Enter to send. @ to mention a teammate — that chooses who answers. Paste or drop a file to attach it."
            : "Enter to send. Ctrl K for commands, @ to bring in a teammate, paste or drop a file to attach it."
        }
        onSend={send}
        onStop={messages.stop}
      />
    </div>
  )
}
