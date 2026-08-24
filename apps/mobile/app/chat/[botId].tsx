import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native"
import { useSafeAreaInsets } from "react-native-safe-area-context"
import { Stack, useLocalSearchParams, useRouter } from "expo-router"
import {
  doneEventText,
  isStreamClosedDone,
  type Bot,
  type ChannelEvent,
  type DesktopEventData,
  type HandoffEventData,
  type Message,
  type Thread,
} from "@nesqbot/protocol"
import {
  bots as botsApi,
  findOrCreateBotThread,
  streamThreadMessage,
  subscribeThreadEvents,
  threads as threadsApi,
} from "../../src/api/endpoints"
import type { EventStreamHandle } from "../../src/api/sse"
import { useAuth } from "../../src/auth"
import { BotAvatar, Button, ErrorView, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useTheme, type Theme } from "../../src/theme"
import { messageTime } from "../../src/utils/format"
import { refreshInbox, removeTakeover, upsertTakeover } from "../../src/state/inbox"
import { takeoverFromDone, takeoverFromEvent, type TakeoverRequest } from "../../src/lib/takeover"

interface ChatItem {
  id: string
  role: "user" | "assistant" | "system" | "tool"
  content: string
  created_at?: string
  /** Local-only user message that has not been confirmed by the API. */
  pending?: boolean
  /** Send failed -- the bubble offers a retry. */
  failed?: boolean
  /** Assistant bubble currently receiving tokens. */
  streaming?: boolean
  /** Inline note (tool ran, handoff, approval created). */
  system?: boolean
}

interface ChatInit {
  bot: Bot
  thread: Thread
  messages: Message[]
}

function toChatItem(message: Message): ChatItem {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    created_at: message.created_at,
  }
}

/** Shown when a turn ended because the bot hit its daily cap; no model was called. */
const BUDGET_BLOCK_NOTE = "Daily budget reached, so no model was called. Raise the cap on the Usage tab."

/**
 * A handoff, in words that say whether a bot *chose* to hand the work over.
 *
 * Routing and delegation share one event, and rendering both as "handed off to
 * Sales" throws away the more interesting half. Delegation means the lead bot
 * decided the sales bot should close this and passed it a brief — that is the
 * team working, and it is the thing worth showing. `chain` is preferred when
 * present because it is the whole path, not just the last hop.
 */
function describeHandoff(data: HandoffEventData): string {
  if (data.delegated !== true) return `Now answering: ${data.bot_name}`
  if (data.chain) return `Delegated · ${data.chain.replace(/_/g, " ")}`
  if (data.from_bot_name) return `${data.from_bot_name} delegated this to ${data.bot_name}`
  return `Delegated to ${data.bot_name}`
}

/**
 * Desktop progress, or null for a phase not worth a line in the transcript.
 *
 * A cold start takes 30–90 seconds, and without this the turn looks hung — the
 * single most common way an agent product reads as broken. `ready` is silent on
 * purpose: "the desktop is up" is only interesting as the end of the wait, and
 * the wait is already on screen.
 */
function describeDesktop(data: DesktopEventData): string | null {
  switch (data.phase) {
    case "starting":
      return data.elapsed_seconds
        ? `Bringing up the desktop — ${data.elapsed_seconds}s so far`
        : "Bringing up the desktop. A cold start takes 30–90 seconds."
    case "unavailable":
      return `The desktop would not start: ${data.detail ?? "no reason given"}`
    case "blocked":
      return data.detail ?? "Starting the desktop needs an approval first."
    case "finished":
      return data.steps ? `Did ${data.steps} ${data.steps === 1 ? "step" : "steps"} on the desktop` : null
    default:
      return null
  }
}

/** One line for the events both channels share. Null means "render nothing". */
function describeEvent(event: ChannelEvent): string | null {
  switch (event.event) {
    case "handoff":
      return describeHandoff(event.data)
    case "tool":
      return `${event.data.connector} · ${event.data.action} ${event.data.ok ? "ran" : "failed"}`
    case "approval":
      // A decision frame carries `phase` and means a parked run just restarted;
      // a bare frame means it just parked. Same event name, opposite news.
      if (event.data.phase === "approved") return "Approved — the bot is carrying on."
      if (event.data.phase === "rejected") return "Rejected — the bot has been told, and is carrying on."
      return `Approval needed: ${event.data.title}`
    case "desktop":
      return describeDesktop(event.data)
    default:
      return null
  }
}

let localCounter = 0
function localId(prefix: string): string {
  localCounter += 1
  return `${prefix}-${Date.now()}-${localCounter}`
}

export default function ChatScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const insets = useSafeAreaInsets()
  const router = useRouter()
  const { botId } = useLocalSearchParams<{ botId: string }>()
  const { status } = useAuth()
  const authenticated = status === "authenticated"

  const [items, setItems] = useState<ChatItem[]>([])
  const [draft, setDraft] = useState("")
  const [streaming, setStreaming] = useState(false)
  const [sendError, setSendError] = useState<unknown>(null)
  const [refreshing, setRefreshing] = useState(false)
  /** Set while a turn we did not initiate is running (from /threads/{id}/events). */
  const [pushedTurnBy, setPushedTurnBy] = useState<string | null>(null)
  /**
   * The bot stopped and wants a person at the screen.
   *
   * Held on the screen rather than only in the inbox because this is where the
   * person is *right now* — being told to go and look somewhere else for the
   * thing that just happened in front of you is the wrong shape.
   */
  const [takeover, setTakeover] = useState<TakeoverRequest | null>(null)

  const scrollRef = useRef<ScrollView>(null)
  const streamRef = useRef<EventStreamHandle | null>(null)

  /**
   * Reuses the bot's existing thread instead of creating a new one on every mount --
   * the original screen spawned a fresh thread each time it opened.
   */
  const load = useCallback(
    async (signal: AbortSignal): Promise<ChatInit> => {
      const bot = await botsApi.get(String(botId), { signal })
      const { thread } = await findOrCreateBotThread(String(botId), bot.name, { signal })
      const messages = await threadsApi.messages(thread.id, { signal })
      return { bot, thread, messages }
    },
    [botId],
  )

  const init = useAsync<ChatInit>(load, [botId], { enabled: authenticated && Boolean(botId) })
  const bot = init.data?.bot ?? null
  const threadId = init.data?.thread.id ?? null

  useEffect(() => {
    if (init.data) setItems(init.data.messages.map(toChatItem))
  }, [init.data])

  useEffect(() => {
    return () => {
      streamRef.current?.close()
      streamRef.current = null
    }
  }, [])

  const scrollToEnd = useCallback((animated = true) => {
    requestAnimationFrame(() => scrollRef.current?.scrollToEnd({ animated }))
  }, [])

  const patchItem = useCallback((id: string, patch: Partial<ChatItem>) => {
    setItems((previous) => previous.map((item) => (item.id === id ? { ...item, ...patch } : item)))
  }, [])

  const appendDelta = useCallback((id: string, delta: string) => {
    setItems((previous) => previous.map((item) => (item.id === id ? { ...item, content: item.content + delta } : item)))
  }, [])

  /**
   * Record a takeover request from whichever route found it, and keep the inbox
   * in step. Null is a no-op rather than a clear: only an explicit non-requested
   * `takeover` phase releases a request, and that is handled at its own case.
   */
  const noteTakeover = useCallback((request: TakeoverRequest | null) => {
    if (!request) return
    setTakeover((current) => (current?.runId === request.runId ? current : request))
    upsertTakeover(request)
    void refreshInbox().catch(() => undefined)
  }, [])

  /**
   * A `takeover` frame whose phase is not `requested` is *releasing* a request:
   * the run was resumed, from this screen or from another device entirely.
   *
   * Clearing it in the inbox as well as on screen matters — otherwise the badge
   * keeps counting a task that is already moving again, and the person opens a
   * Continue button that can only answer `resumed: false`.
   */
  const releaseTakeover = useCallback((runId: string | undefined) => {
    if (!runId) return
    setTakeover((current) => (current?.runId === runId ? null : current))
    removeTakeover(runId)
  }, [])

  const addSystemNote = useCallback((content: string) => {
    setItems((previous) => [...previous, { id: localId("sys"), role: "system", content, system: true }])
  }, [])

  /** Appends a completed assistant message, ignoring one we already hold. */
  const upsertAssistantMessage = useCallback((id: string, content: string) => {
    setItems((previous) => {
      if (previous.some((item) => item.id === id)) return previous
      return [...previous, { id, role: "assistant", content, created_at: new Date().toISOString() }]
    })
  }, [])

  /** Re-reads the canonical message list so local ids are replaced by server ids. */
  const syncMessages = useCallback(async () => {
    if (!threadId) return
    try {
      const fresh = await threadsApi.messages(threadId)
      setItems(fresh.map(toChatItem))
    } catch {
      /* keep the optimistic list; the user can pull to refresh */
    }
  }, [threadId])

  /**
   * Passive channel for turns this phone did not start (worker pushes, routines).
   *
   * `/threads/{id}/events` sends no token deltas -- a turn opens with `turn_started`
   * and its `done` carries the whole message -- so there is nothing to accumulate here.
   * It stays closed while we are driving our own stream, to avoid double-rendering
   * the same turn.
   */
  useEffect(() => {
    if (!threadId || streaming) return undefined

    const handle = subscribeThreadEvents(
      threadId,
      (event) => {
        switch (event.event) {
          case "turn_started":
            setPushedTurnBy(event.data.bot_name || bot?.name || "The bot")
            break
          case "handoff":
          case "tool":
          case "desktop": {
            const note = describeEvent(event)
            if (note) addSystemNote(note)
            break
          }
          case "approval": {
            void refreshInbox().catch(() => undefined)
            const note = describeEvent(event)
            if (note) addSystemNote(note)
            break
          }
          case "takeover": {
            const request = takeoverFromEvent(event.data)
            if (request) noteTakeover(request)
            else releaseTakeover(event.data.run_id)
            setPushedTurnBy(null)
            break
          }
          case "cost":
            // Deliberately not rendered per step: a line of spend after every
            // model call buries the conversation. The Usage tab is where spend
            // is read, and the budget block below is where it becomes urgent.
            break
          case "done": {
            setPushedTurnBy(null)
            // A turn that ended by parking on a person is not a finished turn.
            // If the `takeover` frame was missed — reconnected mid-turn — this
            // recovers the request from the `done` alone.
            noteTakeover(takeoverFromDone(event.data, bot?.name))
            // Two frames share the `done` name. The close-out means the connection
            // ended without a terminal event -- nothing finished, so minting a message
            // from it would invent a reply that never happened.
            if (isStreamClosedDone(event.data)) break
            if (event.data.budget_blocked) addSystemNote(BUDGET_BLOCK_NOTE)
            // The wire field is `message`, not `content`; doneEventText reads either.
            const text = doneEventText(event.data)
            if (text && event.data.message_id) {
              upsertAssistantMessage(event.data.message_id, text)
            } else {
              void syncMessages()
            }
            scrollToEnd()
            break
          }
          case "error":
            setPushedTurnBy(null)
            addSystemNote(event.data.detail)
            break
          case "unknown":
            // An event name this build does not know: ignore rather than break.
            break
        }
      },
      () => setPushedTurnBy(null),
    )

    return () => handle.close()
  }, [
    threadId,
    streaming,
    bot?.name,
    addSystemNote,
    noteTakeover,
    releaseTakeover,
    upsertAssistantMessage,
    syncMessages,
    scrollToEnd,
  ])

  const send = useCallback(
    async (text: string, retryOfId?: string) => {
      if (!threadId || streaming) return
      const content = text.trim()
      if (!content) return

      setSendError(null)
      const userId = retryOfId ?? localId("user")
      const assistantId = localId("assistant")

      setItems((previous) => {
        const withUser = retryOfId
          ? previous.map((item) => (item.id === retryOfId ? { ...item, failed: false, pending: true } : item))
          : [
              ...previous,
              {
                id: userId,
                role: "user" as const,
                content,
                created_at: new Date().toISOString(),
                pending: true,
              },
            ]
        return [
          ...withUser,
          {
            id: assistantId,
            role: "assistant" as const,
            content: "",
            created_at: new Date().toISOString(),
            streaming: true,
          },
        ]
      })

      setStreaming(true)
      scrollToEnd()

      streamRef.current = streamThreadMessage(threadId, content, {
        onEvent: (event) => {
          switch (event.event) {
            case "token":
              appendDelta(assistantId, event.data.delta)
              break
            case "handoff":
            case "tool":
            case "desktop": {
              const note = describeEvent(event)
              if (note) addSystemNote(note)
              break
            }
            case "approval": {
              void refreshInbox().catch(() => undefined)
              const note = describeEvent(event)
              if (note) addSystemNote(note)
              break
            }
            case "takeover": {
              const request = takeoverFromEvent(event.data)
              if (request) {
                noteTakeover(request)
                // The bubble that was filling with tokens is not going to be
                // finished by this turn. Close it out rather than leaving a
                // spinner that never resolves.
                patchItem(assistantId, { streaming: false })
              } else {
                releaseTakeover(event.data.run_id)
              }
              break
            }
            case "cost":
              break
            case "error":
              patchItem(assistantId, {
                streaming: false,
                failed: true,
                content: event.data.detail,
              })
              patchItem(userId, { pending: false, failed: true })
              break
            case "done": {
              // Close-out frame: the connection ended without the turn finishing. Let
              // onClose decide (it will fall back to the plain endpoint); do not treat
              // it as a completed reply.
              if (isStreamClosedDone(event.data)) break
              noteTakeover(takeoverFromDone(event.data, bot?.name))
              if (event.data.budget_blocked) addSystemNote(BUDGET_BLOCK_NOTE)
              // Tokens normally filled the bubble already; `message` is the safety net
              // for a turn that finished without streaming any (budget block, cache).
              const text = doneEventText(event.data)
              setItems((previous) =>
                previous.map((item) => {
                  if (item.id === assistantId) {
                    return {
                      ...item,
                      streaming: false,
                      content: item.content.length > 0 ? item.content : (text ?? ""),
                    }
                  }
                  if (item.id === userId) return { ...item, pending: false }
                  return item
                }),
              )
              break
            }
            case "unknown":
              // An event name this build does not know: ignore rather than break.
              break
          }
          scrollToEnd()
        },
        onClose: ({ fellBack, error }) => {
          streamRef.current = null
          setStreaming(false)
          if (error) {
            setSendError(error)
            patchItem(userId, { pending: false, failed: true })
            patchItem(assistantId, { streaming: false, failed: true })
            return
          }
          patchItem(userId, { pending: false, failed: false })
          patchItem(assistantId, { streaming: false })
          if (fellBack) {
            // The plain endpoint returned the whole turn: pull it from the API.
            setItems((previous) => previous.filter((item) => item.id !== assistantId))
          }
          void syncMessages()
          scrollToEnd()
        },
      })

      if (!retryOfId) setDraft("")
    },
    [
      threadId,
      streaming,
      bot?.name,
      appendDelta,
      addSystemNote,
      noteTakeover,
      releaseTakeover,
      patchItem,
      syncMessages,
      scrollToEnd,
    ],
  )

  const stop = useCallback(() => {
    streamRef.current?.close()
    streamRef.current = null
    setStreaming(false)
    setItems((previous) => previous.map((item) => (item.streaming ? { ...item, streaming: false } : item)))
    // The turn may still have been persisted server-side.
    void syncMessages()
  }, [syncMessages])

  const retry = useCallback(
    (item: ChatItem) => {
      void send(item.content, item.id)
    },
    [send],
  )

  const composerDisabled = !threadId || streaming
  const keyboardOffset = Platform.OS === "ios" ? insets.top + 44 : 0

  const composer = (
    <View style={styles.composer}>
      <TextInput
        style={styles.input}
        placeholder={bot ? `Message ${bot.name}…` : "Message like a teammate…"}
        placeholderTextColor={theme.palette.textDim}
        value={draft}
        onChangeText={setDraft}
        multiline
        editable={Boolean(threadId)}
        accessibilityLabel="Message input"
        onSubmitEditing={() => void send(draft)}
        blurOnSubmit={false}
      />
      {streaming ? (
        <Button label="Stop" variant="ghost" onPress={stop} accessibilityLabel="Stop the bot's reply" />
      ) : (
        <Button
          label="Send"
          onPress={() => void send(draft)}
          disabled={composerDisabled || draft.trim().length === 0}
          accessibilityLabel="Send message"
        />
      )}
    </View>
  )

  return (
    <>
      <Stack.Screen options={{ title: bot?.name ?? "Chat" }} />
      <KeyboardAvoidingView
        style={styles.flex}
        // Android resizes the window itself (softwareKeyboardLayoutMode: resize), so a
        // behavior there would double-adjust the layout.
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        keyboardVerticalOffset={keyboardOffset}
      >
        <Screen
          scroll={false}
          padded={false}
          loading={init.loading}
          loadingLabel="Opening the thread"
          error={init.data === null ? (init.error ?? undefined) : undefined}
          errorTitle="Could not open this chat"
          onRetry={() => void init.reload()}
          footer={init.data ? composer : undefined}
        >
          <ScrollView
            ref={scrollRef}
            style={styles.flex}
            contentContainerStyle={styles.messages}
            keyboardShouldPersistTaps="handled"
            keyboardDismissMode="interactive"
            onContentSizeChange={() => scrollToEnd(false)}
            refreshControl={
              <RefreshControl
                refreshing={refreshing}
                onRefresh={() => {
                  setRefreshing(true)
                  void syncMessages().finally(() => setRefreshing(false))
                }}
                tintColor={theme.palette.accent}
                colors={[theme.palette.accent]}
                progressBackgroundColor={theme.palette.surface}
              />
            }
          >
            {items.length === 0 ? (
              <View style={styles.intro}>
                {bot ? <BotAvatar name={bot.name} slug={bot.slug} size={56} /> : null}
                <Text style={styles.introTitle}>{bot?.name ?? "Bot"}</Text>
                <Text style={styles.introRole}>{bot?.role ?? ""}</Text>
              </View>
            ) : null}

            {items.map((item) => (
              <MessageBubble
                key={item.id}
                item={item}
                bot={bot}
                styles={styles}
                accent={theme.palette.accent}
                onRetry={retry}
              />
            ))}

            {takeover ? (
              <Pressable
                onPress={() => router.push({ pathname: "/takeover/[runId]", params: { runId: takeover.runId } })}
                accessibilityRole="button"
                accessibilityLabel={`${bot?.name ?? "The bot"} needs you at the screen. ${takeover.reason}. ${takeover.whatYouNeed}`}
                accessibilityHint="Opens the live desktop and the Continue button"
                style={({ pressed }) => [styles.takeoverBanner, pressed && styles.takeoverBannerPressed]}
              >
                <Text style={styles.takeoverTag}>NEEDS YOU AT THE SCREEN</Text>
                <Text style={styles.takeoverReason}>{takeover.reason}</Text>
                <Text style={styles.takeoverWhat}>{takeover.whatYouNeed}</Text>
                <Text style={styles.takeoverCta}>Open the screen and continue →</Text>
              </Pressable>
            ) : null}

            {streaming || pushedTurnBy ? (
              <View style={styles.typingRow}>
                <ActivityIndicator size="small" color={theme.palette.accent} />
                <Text style={styles.typing}>{pushedTurnBy ?? bot?.name ?? "The bot"} is working…</Text>
              </View>
            ) : null}

            {sendError ? (
              <ErrorView error={sendError} compact title="Message not delivered" onRetry={() => setSendError(null)} />
            ) : null}
          </ScrollView>
        </Screen>
      </KeyboardAvoidingView>
    </>
  )
}

function MessageBubble({
  item,
  bot,
  styles,
  accent,
  onRetry,
}: {
  item: ChatItem
  bot: Bot | null
  styles: ReturnType<typeof makeStyles>
  accent: string
  onRetry: (item: ChatItem) => void
}): JSX.Element {
  if (item.system || item.role === "system" || item.role === "tool") {
    return (
      <View style={styles.systemRow}>
        <Text style={styles.systemText}>{item.content}</Text>
      </View>
    )
  }

  const isUser = item.role === "user"

  if (isUser) {
    return (
      <View style={styles.userRow}>
        <View style={[styles.bubble, styles.userBubble, item.failed && styles.failedBubble]}>
          <Text style={styles.userText} selectable>
            {item.content}
          </Text>
          <View style={styles.metaRow}>
            <Text style={styles.metaTextOnAccent}>
              {item.failed ? "Not sent" : item.pending ? "Sending…" : messageTime(item.created_at)}
            </Text>
          </View>
        </View>
        {item.failed ? (
          <Pressable
            onPress={() => onRetry(item)}
            accessibilityRole="button"
            accessibilityLabel="Retry sending this message"
            hitSlop={8}
            style={styles.retry}
          >
            <Text style={[styles.retryText, { color: accent }]}>Retry</Text>
          </Pressable>
        ) : null}
      </View>
    )
  }

  return (
    <View style={styles.assistantRow}>
      <BotAvatar name={bot?.name ?? "Bot"} slug={bot?.slug} size={28} />
      <View style={styles.assistantColumn}>
        <Text style={styles.assistantName}>{bot?.name ?? "Assistant"}</Text>
        <View style={[styles.bubble, styles.assistantBubble, item.failed && styles.failedBubble]}>
          {item.content.length > 0 ? (
            <Text style={styles.assistantText} selectable>
              {item.content}
            </Text>
          ) : (
            <ActivityIndicator size="small" color={accent} />
          )}
          {item.streaming ? null : (
            <View style={styles.metaRow}>
              <Text style={styles.metaText}>{messageTime(item.created_at)}</Text>
            </View>
          )}
        </View>
      </View>
    </View>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    flex: { flex: 1 },
    messages: { padding: spacing.lg, gap: spacing.md, flexGrow: 1 },
    intro: { alignItems: "center", gap: spacing.xs, paddingVertical: spacing.xxl },
    introTitle: { color: palette.text, fontSize: 18, fontWeight: "800" },
    introRole: { color: palette.textMuted, fontSize: 13, textAlign: "center" },
    userRow: { alignSelf: "flex-end", alignItems: "flex-end", maxWidth: "88%", gap: 2 },
    assistantRow: {
      flexDirection: "row",
      alignItems: "flex-start",
      gap: spacing.sm,
      maxWidth: "92%",
    },
    assistantColumn: { flex: 1, gap: 2 },
    assistantName: { color: palette.textDim, fontSize: 11, fontWeight: "700" },
    bubble: { borderRadius: radii.lg, padding: spacing.md, gap: spacing.xs },
    userBubble: { backgroundColor: palette.accent },
    assistantBubble: {
      backgroundColor: palette.surface,
      borderWidth: 1,
      borderColor: palette.border,
    },
    failedBubble: { borderWidth: 1, borderColor: palette.danger },
    userText: { color: theme.onAccent, fontSize: 15, lineHeight: 21 },
    assistantText: { color: palette.text, fontSize: 15, lineHeight: 21 },
    metaRow: { flexDirection: "row", justifyContent: "flex-end" },
    metaText: { color: palette.textDim, fontSize: 10 },
    metaTextOnAccent: { color: theme.onAccent, fontSize: 10, opacity: 0.75 },
    retry: { paddingHorizontal: spacing.sm, paddingVertical: spacing.xs, minHeight: 28 },
    retryText: { fontSize: 12, fontWeight: "700" },
    systemRow: { alignSelf: "center", paddingHorizontal: spacing.md },
    systemText: { color: palette.textDim, fontSize: 11, textAlign: "center" },
    takeoverBanner: {
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: palette.accent,
      borderLeftWidth: 3,
      padding: spacing.md,
      gap: spacing.xs,
    },
    takeoverBannerPressed: { backgroundColor: palette.surfacePressed },
    takeoverTag: { ...theme.type.labelCaps, color: palette.accent },
    takeoverReason: { color: palette.text, fontSize: 15, fontWeight: "700", lineHeight: 21 },
    takeoverWhat: { color: palette.textMuted, fontSize: 14, lineHeight: 20 },
    takeoverCta: { color: palette.accent, fontSize: 13, fontWeight: "700" },
    typingRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    typing: { color: palette.textMuted, fontSize: 12 },
    composer: { flexDirection: "row", alignItems: "flex-end", gap: spacing.sm },
    input: {
      flex: 1,
      minHeight: 44,
      maxHeight: 120,
      borderRadius: radii.md,
      paddingHorizontal: spacing.md,
      paddingVertical: 10,
      color: palette.text,
      backgroundColor: palette.surfaceAlt,
      borderWidth: 1,
      borderColor: palette.border,
    },
  })
}
