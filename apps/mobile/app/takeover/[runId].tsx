/**
 * Human takeover — "I've finished, continue".
 *
 * The agent drove until it hit something only a person can do: a login, an MFA
 * prompt, a CAPTCHA. It parked the run in `awaiting_human` with everything
 * needed to carry on written to the row, and it is waiting. This screen is the
 * other half of that handshake:
 *
 *   1. say what it needs, in the bot's own words;
 *   2. show the live screen so the person can actually do it;
 *   3. one button — `POST /runs/{run_id}/resume` — and the *same task* carries
 *      on, with a fresh screenshot so the agent can see what changed.
 *
 * Three behaviours here are not cosmetic:
 *
 * - **`resumed: false` is not an error.** The API claims the run with a single
 *   conditional status update, so a double-press loses the race and is told so
 *   rather than starting a second agent loop against the browser session the
 *   person just authenticated. Showing an error there would train people to
 *   press twice, which is the exact thing the idempotency is defending against.
 * - **The desktop must be running.** Resume deliberately does *not* auto-start a
 *   stopped one: the whole value of the resume is the session the human just
 *   signed into, and a restart takes the container filesystem with it. So this
 *   screen refuses to pretend, and says the login is gone.
 * - **Interaction is opt-in.** The viewer is `view_only` until the person turns
 *   takeover on. A stray tap on a live, signed-in browser is not a gesture
 *   anyone intended.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ActivityIndicator,
  Image,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
  type GestureResponderEvent,
  type LayoutChangeEvent,
} from "react-native"
import { WebView } from "react-native-webview"
import { Stack, useLocalSearchParams, useRouter } from "expo-router"
import type { Bot, BotDesktop, DesktopScreenshot, ResumeRunOut, Run } from "../../src/api/types"
import { bots as botsApi, desktop as desktopApi, runs as runsApi } from "../../src/api/endpoints"
import { ApiError, getApiBaseUrl } from "../../src/api/client"
import { useAuth } from "../../src/auth"
import { Badge, Button, Card, ErrorView, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useFocusPolling } from "../../src/hooks/usePolling"
import { desktopStreamUrl } from "../../src/lib/desktopStream"
import { takeoverFromRun, type TakeoverRequest } from "../../src/lib/takeover"
import { refreshInbox, removeTakeover } from "../../src/state/inbox"
import { useTheme, type Theme } from "../../src/theme"
import { relativeAge } from "../../src/utils/format"

const SCREENSHOT_POLL_MS = 2500

interface TakeoverInit {
  run: Run
  request: TakeoverRequest | null
  bot: Bot | null
  desktop: BotDesktop | null
}

export default function TakeoverScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const router = useRouter()
  const { runId } = useLocalSearchParams<{ runId: string }>()
  const { status } = useAuth()
  const authenticated = status === "authenticated"

  const [note, setNote] = useState("")
  const [resuming, setResuming] = useState(false)
  const [outcome, setOutcome] = useState<ResumeRunOut | null>(null)
  const [resumeError, setResumeError] = useState<unknown>(null)
  const [interactive, setInteractive] = useState(false)
  const [streamUrl, setStreamUrl] = useState<string | null>(null)
  const [streamFailed, setStreamFailed] = useState(false)
  const [shot, setShot] = useState<DesktopScreenshot | null>(null)
  const [shotError, setShotError] = useState<unknown>(null)
  const [frameWidth, setFrameWidth] = useState(0)
  const [startingDesktop, setStartingDesktop] = useState(false)
  const shotInFlight = useRef(false)

  const load = useCallback(
    async (signal: AbortSignal): Promise<TakeoverInit> => {
      const run = await runsApi.get(String(runId), { signal })
      const request = takeoverFromRun(run)
      // The bot and its desktop are context, not the point. Either failing must
      // not stop the person from pressing Continue.
      const [bot, desktop] = await Promise.all([
        botsApi.get(run.bot_id, { signal }).catch((): Bot | null => null),
        desktopApi.get(run.bot_id, { signal }).catch((): BotDesktop | null => null),
      ])
      return { run, request, bot, desktop }
    },
    [runId],
  )

  const init = useAsync<TakeoverInit>(load, [runId], { enabled: authenticated && Boolean(runId) })

  const run = init.data?.run ?? null
  const request = init.data?.request ?? null
  const bot = init.data?.bot ?? null
  const desktopState = init.data?.desktop ?? null
  const desktopRunning = desktopState?.state === "running"
  const botId = run?.bot_id ?? null

  /** Still parked, or has it moved on while this screen was open? */
  const parked = run?.status === "awaiting_human"
  const resumedOk = outcome?.resumed === true
  const alreadyMoving = outcome !== null && outcome.resumed === false

  /* ------------------------------------------------------------ the stream */

  /**
   * Mint a viewing ticket, then hand noVNC the proxied URL.
   *
   * Re-minted whenever the person toggles interactivity, because `view_only` is
   * baked into the URL noVNC loads and the control socket burns the ticket the
   * moment it connects — the old one cannot be reused for a second page load.
   */
  const openStream = useCallback(
    async (viewOnly: boolean) => {
      if (!botId) return
      setStreamFailed(false)
      try {
        const ticket = await desktopApi.streamTicket(botId)
        setStreamUrl(desktopStreamUrl(ticket, { viewOnly, base: getApiBaseUrl() }))
      } catch {
        // No stream: fall through to the screenshot poller, which is a worse
        // view but an honest one. Not surfaced as an error — the person still
        // has a Continue button, and that is the point of the screen.
        setStreamFailed(true)
        setStreamUrl(null)
      }
    },
    [botId],
  )

  useEffect(() => {
    if (!desktopRunning) {
      setStreamUrl(null)
      return
    }
    void openStream(!interactive)
  }, [desktopRunning, interactive, openStream])

  const showScreenshot = desktopRunning && (streamFailed || streamUrl === null)

  const pollScreenshot = useCallback(async () => {
    if (!botId || shotInFlight.current) return
    shotInFlight.current = true
    try {
      setShot(await desktopApi.screenshot(botId))
      setShotError(null)
    } catch (caught) {
      setShotError(caught)
    } finally {
      shotInFlight.current = false
    }
  }, [botId])

  useEffect(() => {
    if (showScreenshot) void pollScreenshot()
  }, [showScreenshot, pollScreenshot])
  useFocusPolling(pollScreenshot, SCREENSHOT_POLL_MS, showScreenshot)

  const onFrameLayout = useCallback((event: LayoutChangeEvent) => {
    setFrameWidth(event.nativeEvent.layout.width)
  }, [])

  const displayHeight = shot && shot.width > 0 ? Math.round((frameWidth * shot.height) / shot.width) : 0

  /** Tap-to-click on the screenshot fallback, only while takeover is on. */
  const onFramePress = useCallback(
    (event: GestureResponderEvent) => {
      if (!interactive || !botId || !shot || frameWidth === 0 || displayHeight === 0) return
      const { locationX, locationY } = event.nativeEvent
      void desktopApi
        .action(botId, {
          action: "click",
          x: Math.round((locationX / frameWidth) * shot.width),
          y: Math.round((locationY / displayHeight) * shot.height),
          button: "left",
        })
        .then(() => pollScreenshot())
        .catch((caught: unknown) => setShotError(caught))
    },
    [interactive, botId, shot, frameWidth, displayHeight, pollScreenshot],
  )

  /* --------------------------------------------------------------- resume */

  const resume = useCallback(async () => {
    if (!run) return
    setResuming(true)
    setResumeError(null)
    try {
      const result = await runsApi.resume(run.id, note)
      setOutcome(result)
      // The run is no longer waiting on a person either way — it is running, or
      // it already was. Drop it from the inbox now rather than at the next poll.
      removeTakeover(run.id)
      void refreshInbox().catch(() => undefined)
      void init.refresh()
    } catch (caught) {
      setResumeError(caught)
      // 409 `run_not_resumable`, or a 404 because it moved on: re-read the run
      // rather than leaving a button the person will keep pressing.
      if (caught instanceof ApiError) void init.refresh()
    } finally {
      setResuming(false)
    }
  }, [run, note, init])

  const startDesktop = useCallback(async () => {
    if (!botId || !run) return
    setStartingDesktop(true)
    try {
      const next = await desktopApi.start(botId)
      // Patch the loaded state rather than casting a possibly-absent one into
      // shape: the Start button only renders inside the loaded view, and a cast
      // asserting that would fail silently the day it stops being true.
      init.setData((previous) => (previous ? { ...previous, desktop: next } : { run, request, bot, desktop: next }))
    } catch (caught) {
      setShotError(caught)
    } finally {
      setStartingDesktop(false)
    }
  }, [botId, run, request, bot, init])

  /* ----------------------------------------------------------------- view */

  /**
   * The heading follows the run's actual state. After a resume it must stop
   * saying the bot needs you — it does not any more, and leaving the words up
   * is how someone ends up pressing Continue a second time.
   */
  const title = resumedOk
    ? bot
      ? `${bot.name} is back at it`
      : "Back at it"
    : parked
      ? bot
        ? `${bot.name} needs you`
        : "Needs you"
      : "Nothing to do here"

  return (
    <>
      <Stack.Screen options={{ title: bot?.name ?? "Takeover" }} />
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        keyboardVerticalOffset={Platform.OS === "ios" ? 90 : 0}
      >
        <Screen
          scroll={false}
          padded={false}
          loading={init.loading}
          loadingLabel="Finding the parked task"
          error={init.data === null ? (init.error ?? undefined) : undefined}
          errorTitle="Could not load this handover"
          onRetry={() => void init.reload()}
          testID="takeover-screen"
        >
          <ScrollView style={styles.flex} contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
            {/* ---- what it needs ---- */}
            <Card>
              <View style={styles.headerRow}>
                <Badge
                  label={parked ? "WAITING FOR YOU" : String(run?.status ?? "unknown").toUpperCase()}
                  tone={parked ? "accent" : "neutral"}
                />
                <Text style={styles.age}>{relativeAge(request?.askedAt ?? run?.updated_at)}</Text>
              </View>
              <Text style={styles.title}>{title}</Text>
              <Text style={styles.reason}>{request?.reason ?? "This task stopped and is waiting on a person."}</Text>
              {request?.whatYouNeed ? (
                <View style={styles.instruction}>
                  <Text style={styles.instructionLabel}>WHAT TO DO</Text>
                  <Text style={styles.instructionText}>{request.whatYouNeed}</Text>
                </View>
              ) : null}
              {request?.goal ? <Text style={styles.meta}>Working on: {request.goal}</Text> : null}
            </Card>

            {/* ---- the screen ---- */}
            {!desktopRunning ? (
              <Card
                title="The desktop is not running"
                subtitle={
                  desktopState?.state === "suspended"
                    ? "Resume it to finish the step."
                    : "Resume deliberately does not start one for you."
                }
              >
                <Text style={styles.meta}>
                  The value of this handover is the session you are about to sign into, and starting a fresh machine
                  takes the filesystem — and any login on it — with it. If the desktop was stopped since the bot asked,
                  that session is already gone and continuing would work from a screen that means nothing.
                </Text>
                <Button
                  label={desktopState?.state === "suspended" ? "Resume the desktop" : "Start the desktop"}
                  onPress={() => void startDesktop()}
                  loading={startingDesktop}
                  variant="secondary"
                  accessibilityLabel="Start this bot's desktop"
                />
              </Card>
            ) : (
              <View style={styles.viewport} onLayout={onFrameLayout}>
                {streamUrl && !streamFailed ? (
                  <View style={[styles.frame, { height: Math.max(280, Math.round(frameWidth * 0.68)) }]}>
                    <WebView
                      // Re-mount on a new ticket: a WebView handed a fresh URL for
                      // the same origin can otherwise reuse the burned socket.
                      key={streamUrl}
                      source={{ uri: streamUrl }}
                      style={styles.webview}
                      originWhitelist={["*"]}
                      javaScriptEnabled
                      domStorageEnabled
                      startInLoadingState
                      renderLoading={() => (
                        <View style={styles.placeholder}>
                          <ActivityIndicator size="large" color={theme.palette.accent} />
                        </View>
                      )}
                      onError={() => setStreamFailed(true)}
                      onHttpError={() => setStreamFailed(true)}
                      onRenderProcessGone={() => setStreamFailed(true)}
                    />
                  </View>
                ) : shot ? (
                  <Pressable
                    onPress={onFramePress}
                    disabled={!interactive}
                    accessibilityRole={interactive ? "button" : "image"}
                    accessibilityLabel={
                      interactive
                        ? "The bot's screen. Tap to click at that position."
                        : "The bot's screen, watching only."
                    }
                    style={[styles.frame, { height: displayHeight || 260 }]}
                  >
                    <Image
                      source={{ uri: `data:image/png;base64,${shot.png_base64}` }}
                      style={styles.image}
                      resizeMode="contain"
                      accessibilityIgnoresInvertColors
                    />
                  </Pressable>
                ) : shotError ? (
                  <ErrorView
                    error={shotError}
                    compact
                    title="No picture of the screen"
                    onRetry={() => void pollScreenshot()}
                  />
                ) : (
                  <View style={styles.placeholder}>
                    <ActivityIndicator size="large" color={theme.palette.accent} />
                    <Text style={styles.placeholderText}>Waiting for the first frame…</Text>
                  </View>
                )}

                {streamFailed ? (
                  <Text style={styles.notice}>
                    The live stream would not load, so this is the screenshot fallback — slower, but it is the same
                    machine.{" "}
                    <Text style={styles.link} onPress={() => void openStream(!interactive)} accessibilityRole="button">
                      Try the stream again
                    </Text>
                  </Text>
                ) : null}
              </View>
            )}

            {desktopRunning ? (
              <Card
                title="Take over the screen"
                subtitle="Off means watching only, so a stray tap cannot click something."
                right={
                  <Switch
                    value={interactive}
                    onValueChange={setInteractive}
                    accessibilityLabel="Take over the bot's screen"
                    trackColor={{ true: theme.palette.accent, false: theme.palette.border }}
                    thumbColor={theme.palette.surface}
                  />
                }
              >
                <Text style={styles.meta}>
                  {interactive
                    ? "You are driving. Risky actions are still classified and still held for approval, even now."
                    : "Turn this on to type and click. A password you enter goes to the bot's browser, not to Nesq Bot."}
                </Text>
              </Card>
            ) : null}

            {/* ---- the note ---- */}
            {parked && !resumedOk ? (
              <Card title="Note" subtitle="Optional. Goes into the transcript the bot picks back up from.">
                <TextInput
                  style={styles.input}
                  value={note}
                  onChangeText={setNote}
                  placeholder="e.g. signed in, two-factor done"
                  placeholderTextColor={theme.palette.textDim}
                  multiline
                  maxLength={2000}
                  accessibilityLabel="Note for the bot"
                  accessibilityHint="Never type a password here. It is stored with the run."
                />
                <Text style={styles.warning}>Never put a password or a code in this box.</Text>
              </Card>
            ) : null}

            {/* ---- what happened ---- */}
            {outcome ? (
              <Card
                title={resumedOk ? "Back to work" : "Already going"}
                subtitle={
                  resumedOk
                    ? "The bot picked the same task back up from where it stopped."
                    : "Something had already continued this run, so nothing was started twice."
                }
              >
                <Badge label={resumedOk ? "RESUMED" : "NO CHANGE"} tone={resumedOk ? "success" : "neutral"} />
                {outcome.message ? <Text style={styles.reason}>{outcome.message}</Text> : null}
                {outcome.detail ? <Text style={styles.meta}>{outcome.detail}</Text> : null}
                {outcome.outcome ? <Text style={styles.meta}>Ended: {outcome.outcome}</Text> : null}
                {outcome.approval_id ? (
                  <Button
                    label="It needs an approval now"
                    size="sm"
                    onPress={() =>
                      router.replace({ pathname: "/approvals/[id]", params: { id: String(outcome.approval_id) } })
                    }
                    accessibilityLabel="Open the approval the resumed task is now waiting on"
                  />
                ) : null}
                {outcome.thread_id && bot ? (
                  <Button
                    label="Open the conversation"
                    size="sm"
                    variant="ghost"
                    onPress={() => router.push({ pathname: "/chat/[botId]", params: { botId: bot.id } })}
                    accessibilityLabel={`Open the chat with ${bot.name}`}
                  />
                ) : null}
              </Card>
            ) : null}

            {resumeError ? (
              <ErrorView
                error={resumeError}
                compact
                title="Could not continue the task"
                onRetry={() => setResumeError(null)}
              />
            ) : null}

            {!parked && !outcome ? (
              <Text style={styles.meta}>
                This run is {run?.status ?? "gone"} — it is not waiting on anyone any more.
              </Text>
            ) : null}
          </ScrollView>

          {/* The primary action sits in the pinned footer: it is the one thing
              this screen is for, and it must be under a thumb without scrolling
              past a live desktop viewer to reach it. */}
          {parked && !resumedOk ? (
            <View style={styles.footer}>
              <Button
                label={alreadyMoving ? "Try Continue again" : "Continue the task"}
                block
                onPress={() => void resume()}
                loading={resuming}
                disabled={resuming}
                accessibilityLabel="I have finished. Continue the task."
                accessibilityHint="The bot takes a fresh screenshot and carries on from where it stopped."
              />
              <Text style={styles.footnote}>
                {resuming
                  ? "The bot is looking at the screen and picking the task back up. This can take a minute."
                  : "Only press this once you have finished the step on the screen above."}
              </Text>
            </View>
          ) : null}
        </Screen>
      </KeyboardAvoidingView>
    </>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing, type } = theme
  return StyleSheet.create({
    flex: { flex: 1 },
    content: { padding: spacing.lg, gap: spacing.md, paddingBottom: spacing.xxl },
    headerRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm },
    title: { color: palette.text, fontWeight: "800", fontSize: 20, lineHeight: 26 },
    reason: { color: palette.text, fontSize: 15, lineHeight: 22 },
    meta: { color: palette.textMuted, fontSize: 13, lineHeight: 19 },
    age: { color: palette.textDim, fontSize: 12 },
    warning: { color: palette.warning, fontSize: 12, lineHeight: 17 },
    notice: { color: palette.warning, fontSize: 12, lineHeight: 18 },
    link: { color: palette.accent, fontWeight: "700" },
    instruction: {
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
      padding: spacing.md,
      gap: spacing.xs,
    },
    instructionLabel: { ...type.labelCaps, color: palette.accent },
    instructionText: { color: palette.text, fontSize: 15, lineHeight: 22 },
    viewport: { gap: spacing.sm },
    frame: {
      width: "100%",
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
      overflow: "hidden",
    },
    webview: { flex: 1, backgroundColor: palette.surfaceAlt },
    image: { width: "100%", height: "100%" },
    placeholder: {
      minHeight: 220,
      alignItems: "center",
      justifyContent: "center",
      gap: spacing.sm,
      padding: spacing.xl,
      backgroundColor: palette.surface,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
    },
    placeholderText: { color: palette.textMuted, textAlign: "center", fontSize: 13, lineHeight: 19 },
    input: {
      minHeight: 72,
      maxHeight: 160,
      color: palette.text,
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
      padding: spacing.md,
      textAlignVertical: "top",
    },
    footer: { gap: spacing.sm },
    footnote: { color: palette.textDim, fontSize: 12, lineHeight: 17, textAlign: "center" },
  })
}
