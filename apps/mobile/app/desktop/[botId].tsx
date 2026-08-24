import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ActivityIndicator,
  Image,
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
import type { Bot, BotDesktop, BotDesktopState } from "@nesqbot/protocol"
import { bots as botsApi, desktop as desktopApi } from "../../src/api/endpoints"
import { desktopStreamUrl } from "../../src/lib/desktopStream"
import {
  asPendingApproval,
  type DesktopActionInput,
  type DesktopScreenshot,
  type PendingApprovalOut,
} from "../../src/api/types"
import { errorMessage, getApiBaseUrl } from "../../src/api/client"
import { useAuth } from "../../src/auth"
import { Badge, Button, Card, ErrorView, RiskBadge, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useFocusPolling } from "../../src/hooks/usePolling"
import { useTheme, type Theme } from "../../src/theme"
import { humanize } from "../../src/utils/format"

const STATE_POLL_MS = 5000
const SCREENSHOT_POLL_MS = 2500

/**
 * Watching a Bot Desktop, and why this screen is watch-first.
 *
 * A 1080p Linux desktop scaled to a 390pt-wide phone is not a workstation, and
 * precise mouse work through a touch screen is worse than not having it. But
 * dropping the viewer entirely is wrong too: when a bot asks you to approve
 * "click Send in this window", you have to be able to see the window. So this
 * screen exists as **evidence for a decision** — look, take over in an
 * emergency — and driving a routine through it is a desktop job.
 *
 * The one thing that was outright broken: it pointed a WebView at
 * `BotDesktop.stream_url`, which is a `10.60.x.x` address inside the Azure VNet.
 * No phone can route to it on any network, so the live stream failed every
 * single time and silently fell through to the screenshot poller. It now mints
 * a ticket and loads the API's own proxy, which is the supported route — see
 * `src/lib/desktopStream.ts`.
 */

interface DesktopInit {
  bot: Bot | null
  state: BotDesktop
}

type Lifecycle = "start" | "stop" | "suspend" | "resume"

function stateTone(state: BotDesktopState | string): "success" | "warning" | "danger" | "neutral" {
  switch (state) {
    case "running":
      return "success"
    case "starting":
    case "stopping":
    case "suspended":
      return "warning"
    case "error":
      return "danger"
    default:
      return "neutral"
  }
}

export default function DesktopScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const router = useRouter()
  const { botId } = useLocalSearchParams<{ botId: string }>()
  const { status } = useAuth()
  const authenticated = status === "authenticated"

  const [busy, setBusy] = useState<Lifecycle | null>(null)
  const [actionError, setActionError] = useState<unknown>(null)
  const [webViewFailed, setWebViewFailed] = useState(false)
  const [streamUrl, setStreamUrl] = useState<string | null>(null)
  const [takeover, setTakeover] = useState(false)
  const [typeText, setTypeText] = useState("")
  const [shot, setShot] = useState<DesktopScreenshot | null>(null)
  const [shotError, setShotError] = useState<unknown>(null)
  const [pendingApproval, setPendingApproval] = useState<PendingApprovalOut | null>(null)
  const [frameWidth, setFrameWidth] = useState(0)
  const shotInFlight = useRef(false)

  const load = useCallback(
    async (signal: AbortSignal): Promise<DesktopInit> => {
      const [bot, state] = await Promise.all([
        botsApi.get(String(botId), { signal }).catch((): Bot | null => null),
        desktopApi.get(String(botId), { signal }),
      ])
      return { bot, state }
    },
    [botId],
  )

  const init = useAsync<DesktopInit>(load, [botId], {
    enabled: authenticated && Boolean(botId),
  })

  const desktopState = init.data?.state ?? null
  const bot = init.data?.bot ?? null
  const running = desktopState?.state === "running"
  const transitioning = desktopState?.state === "starting" || desktopState?.state === "stopping"

  // Keep the lifecycle state honest while the screen is open.
  useFocusPolling(() => init.refresh(), STATE_POLL_MS, authenticated && Boolean(botId))

  /**
   * Mint a viewing ticket and build the proxied noVNC URL.
   *
   * `viewOnly` is baked into the URL, and the control socket burns the ticket
   * when it connects, so toggling takeover has to mint a fresh one rather than
   * reuse the last.
   */
  const openStream = useCallback(
    async (viewOnly: boolean) => {
      if (!botId) return
      setWebViewFailed(false)
      try {
        const ticket = await desktopApi.streamTicket(String(botId))
        setStreamUrl(desktopStreamUrl(ticket, { viewOnly, base: getApiBaseUrl() }))
      } catch {
        // The screenshot poller below is the fallback. Not raised as an error:
        // the person still gets a picture of the machine, which is what they
        // came for.
        setWebViewFailed(true)
        setStreamUrl(null)
      }
    },
    [botId],
  )

  useEffect(() => {
    if (!running) {
      setStreamUrl(null)
      return
    }
    void openStream(!takeover)
  }, [running, takeover, openStream])

  // The screenshot fallback: used when there is no stream URL at all, or when the
  // KasmVNC WebView failed to load (blocked origin, self-signed cert, no network route).
  const useScreenshot = running && (!streamUrl || webViewFailed)

  const pollScreenshot = useCallback(async () => {
    if (shotInFlight.current) return
    shotInFlight.current = true
    try {
      const next = await desktopApi.screenshot(String(botId))
      setShot(next)
      setShotError(null)
    } catch (caught) {
      setShotError(caught)
    } finally {
      shotInFlight.current = false
    }
  }, [botId])

  useEffect(() => {
    if (!useScreenshot) return
    void pollScreenshot()
  }, [useScreenshot, pollScreenshot])

  useFocusPolling(pollScreenshot, SCREENSHOT_POLL_MS, useScreenshot)

  // A restart should give the WebView another chance.
  useEffect(() => {
    if (!running) setWebViewFailed(false)
  }, [running])

  useEffect(() => {
    if (!running) setTakeover(false)
  }, [running])

  const runLifecycle = useCallback(
    async (which: Lifecycle) => {
      setBusy(which)
      setActionError(null)
      try {
        const next =
          which === "start"
            ? await desktopApi.start(String(botId))
            : which === "stop"
              ? await desktopApi.stop(String(botId))
              : which === "suspend"
                ? await desktopApi.suspend(String(botId))
                : await desktopApi.resume(String(botId))
        init.setData((previous) => ({ bot: previous?.bot ?? null, state: next }))
        if (which === "start") setWebViewFailed(false)
      } catch (caught) {
        setActionError(caught)
      } finally {
        setBusy(null)
      }
    },
    [botId, init],
  )

  const sendAction = useCallback(
    async (input: DesktopActionInput) => {
      setActionError(null)
      setPendingApproval(null)
      try {
        const result = await desktopApi.action(String(botId), input)
        // A gated action answers 201 PendingApprovalOut instead of the executed-action
        // shape, so branch on approval_id rather than on any field of the success shape.
        setPendingApproval(asPendingApproval(result))
        if (useScreenshot) void pollScreenshot()
      } catch (caught) {
        setActionError(caught)
      }
    },
    [botId, useScreenshot, pollScreenshot],
  )

  const onFrameLayout = useCallback((event: LayoutChangeEvent) => {
    setFrameWidth(event.nativeEvent.layout.width)
  }, [])

  const displayHeight = shot && shot.width > 0 ? Math.round((frameWidth * shot.height) / shot.width) : 0

  const onFramePress = useCallback(
    (event: GestureResponderEvent) => {
      if (!takeover || !shot || frameWidth === 0 || displayHeight === 0) return
      const { locationX, locationY } = event.nativeEvent
      const x = Math.round((locationX / frameWidth) * shot.width)
      const y = Math.round((locationY / displayHeight) * shot.height)
      void sendAction({ action: "click", x, y, button: "left" })
    },
    [takeover, shot, frameWidth, displayHeight, sendAction],
  )

  const lifecycleButtons = (
    <View style={styles.controls}>
      <Button
        label={running ? "Restart" : "Start"}
        size="sm"
        onPress={() => void runLifecycle("start")}
        loading={busy === "start"}
        disabled={busy !== null || transitioning}
        accessibilityLabel={running ? "Restart the bot desktop" : "Start the bot desktop"}
        style={styles.control}
      />
      <Button
        label={desktopState?.state === "suspended" ? "Resume" : "Suspend"}
        size="sm"
        variant="secondary"
        onPress={() => void runLifecycle(desktopState?.state === "suspended" ? "resume" : "suspend")}
        loading={busy === "suspend" || busy === "resume"}
        disabled={busy !== null || (!running && desktopState?.state !== "suspended")}
        accessibilityLabel={desktopState?.state === "suspended" ? "Resume the bot desktop" : "Suspend the bot desktop"}
        style={styles.control}
      />
      <Button
        label="Stop"
        size="sm"
        variant="ghost"
        onPress={() => void runLifecycle("stop")}
        loading={busy === "stop"}
        disabled={busy !== null || desktopState?.state === "absent"}
        accessibilityLabel="Stop the bot desktop"
        style={styles.control}
      />
    </View>
  )

  return (
    <>
      <Stack.Screen options={{ title: bot ? `${bot.name} desktop` : "Bot Desktop" }} />
      <Screen
        scroll={false}
        padded={false}
        loading={init.loading}
        loadingLabel="Checking the desktop"
        error={init.data === null ? (init.error ?? undefined) : undefined}
        errorTitle="Could not reach the Bot Desktop"
        onRetry={() => void init.reload()}
      >
        <ScrollView style={styles.flex} contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
          <Card>
            <View style={styles.statusRow}>
              <Badge
                label={String(desktopState?.state ?? "unknown").toUpperCase()}
                tone={stateTone(desktopState?.state ?? "absent")}
                accessibilityLabel={`Desktop state: ${desktopState?.state ?? "unknown"}`}
              />
              {transitioning ? <ActivityIndicator size="small" color={theme.palette.accent} /> : null}
              {desktopState?.container_id ? (
                <Text style={styles.meta} numberOfLines={1}>
                  {desktopState.container_id.slice(0, 12)}
                </Text>
              ) : null}
            </View>
            {desktopState?.last_error ? <Text style={styles.error}>{desktopState.last_error}</Text> : null}
            {lifecycleButtons}
          </Card>

          {actionError ? (
            <ErrorView error={actionError} compact title="That did not work" onRetry={() => setActionError(null)} />
          ) : null}

          {pendingApproval ? (
            <Card
              title="Approval required"
              subtitle="That action is risk-gated, so it was held instead of run."
              right={<RiskBadge risk={pendingApproval.risk} />}
            >
              <Text style={styles.approvalTitle}>{pendingApproval.title}</Text>
              {pendingApproval.detail ? <Text style={styles.meta}>{pendingApproval.detail}</Text> : null}
              {/* approval_id is nullable on PendingApprovalOut: the action can be
                  held before the row id is available. Send the user to the list then. */}
              {pendingApproval.approval_id ? (
                <Button
                  label="Open the approval"
                  size="sm"
                  onPress={() =>
                    router.push({
                      pathname: "/approvals/[id]",
                      params: { id: pendingApproval.approval_id as string },
                    })
                  }
                  accessibilityLabel={`Open the pending approval: ${pendingApproval.title}`}
                />
              ) : (
                <Button
                  label="Open the inbox"
                  size="sm"
                  variant="secondary"
                  onPress={() => router.push("/")}
                  accessibilityLabel="Open the inbox"
                />
              )}
            </Card>
          ) : null}

          <View style={styles.viewport} onLayout={onFrameLayout}>
            {!running ? (
              <View style={styles.placeholder}>
                <Text style={styles.placeholderText}>
                  {desktopState?.state === "suspended"
                    ? "The desktop is suspended. Resume it to watch or take over."
                    : "Start the desktop to watch this bot work, or to sign in on its behalf."}
                </Text>
              </View>
            ) : streamUrl && !webViewFailed ? (
              <View style={[styles.frame, { height: Math.max(240, Math.round(frameWidth * 0.62)) }]}>
                <WebView
                  // Re-mount on a new ticket: reusing the component would keep
                  // the burned socket from the previous one.
                  key={streamUrl}
                  source={{ uri: streamUrl }}
                  style={styles.webview}
                  originWhitelist={["*"]}
                  javaScriptEnabled
                  domStorageEnabled
                  allowsInlineMediaPlayback
                  mediaPlaybackRequiresUserAction={false}
                  startInLoadingState
                  renderLoading={() => (
                    <View style={styles.placeholder}>
                      <ActivityIndicator size="large" color={theme.palette.accent} />
                    </View>
                  )}
                  onError={() => setWebViewFailed(true)}
                  onHttpError={() => setWebViewFailed(true)}
                  onRenderProcessGone={() => setWebViewFailed(true)}
                />
              </View>
            ) : shot ? (
              <Pressable
                onPress={onFramePress}
                disabled={!takeover}
                accessibilityRole={takeover ? "button" : "image"}
                accessibilityLabel={
                  takeover ? "Bot desktop screenshot. Tap to click at that position." : "Bot desktop screenshot"
                }
                style={[styles.frame, { height: displayHeight || 240 }]}
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
                title="No picture of the desktop"
                onRetry={() => void pollScreenshot()}
              />
            ) : (
              <View style={styles.placeholder}>
                <ActivityIndicator size="large" color={theme.palette.accent} />
                <Text style={styles.placeholderText}>Waiting for the first frame…</Text>
              </View>
            )}
          </View>

          {running && webViewFailed ? (
            <Text style={styles.notice}>
              The live stream would not load, so this is the screenshot fallback — slower, but it is the same machine.{" "}
              <Text style={styles.link} onPress={() => void openStream(!takeover)} accessibilityRole="button">
                Try the stream again
              </Text>
            </Text>
          ) : null}

          {running ? (
            <Card
              title="Drive it yourself"
              subtitle="Turn this on to send clicks and typing to the bot's desktop."
              right={
                <Switch
                  value={takeover}
                  onValueChange={setTakeover}
                  accessibilityLabel="Take over the bot desktop"
                  trackColor={{ true: theme.palette.accent, false: theme.palette.border }}
                  thumbColor={theme.palette.surface}
                />
              }
            >
              {takeover ? (
                <>
                  <Text style={styles.meta}>
                    {useScreenshot
                      ? "Tap the screenshot to click at that spot."
                      : "Switch to the screenshot view to tap-to-click."}
                  </Text>
                  <View style={styles.typeRow}>
                    <TextInput
                      style={styles.input}
                      value={typeText}
                      onChangeText={setTypeText}
                      placeholder="Text to type on the desktop"
                      placeholderTextColor={theme.palette.textDim}
                      autoCapitalize="none"
                      autoCorrect={false}
                      accessibilityLabel="Text to type on the bot desktop"
                    />
                    <Button
                      label="Type"
                      size="sm"
                      onPress={() => {
                        const text = typeText
                        setTypeText("")
                        void sendAction({ action: "type", text })
                      }}
                      disabled={typeText.length === 0}
                      accessibilityLabel="Send this text to the bot desktop"
                    />
                  </View>
                  <View style={styles.keyRow}>
                    {["Enter", "Tab", "Escape"].map((key) => (
                      <Button
                        key={key}
                        label={key}
                        size="sm"
                        variant="ghost"
                        onPress={() => void sendAction({ action: "key", keys: [key.toLowerCase()] })}
                        accessibilityLabel={`Press ${key} on the bot desktop`}
                      />
                    ))}
                  </View>
                </>
              ) : (
                <Text style={styles.meta}>
                  Watching only. Risky actions (send, spend, delete) are still classified and still held for approval
                  even when you are driving — being at the keyboard is not authority.
                </Text>
              )}
            </Card>
          ) : null}

          {init.error && init.data !== null ? (
            <Text style={styles.notice}>Live state may be stale: {errorMessage(init.error)}</Text>
          ) : null}

          <Text style={styles.footnote}>
            {humanize(bot?.desktop_profile ?? "")} profile
            {desktopState?.control_url ? " · control channel available" : ""}
          </Text>
        </ScrollView>
      </Screen>
    </>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    flex: { flex: 1 },
    content: { padding: spacing.lg, gap: spacing.md, paddingBottom: spacing.xxl },
    statusRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    meta: { color: palette.textMuted, fontSize: 12, flexShrink: 1 },
    error: { color: palette.danger, fontSize: 13 },
    notice: { color: palette.warning, fontSize: 12, lineHeight: 18 },
    link: { color: palette.accent, fontWeight: "700" },
    controls: { flexDirection: "row", gap: spacing.sm },
    control: { flex: 1 },
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
      minHeight: 200,
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
    typeRow: { flexDirection: "row", gap: spacing.sm, alignItems: "center" },
    keyRow: { flexDirection: "row", gap: spacing.sm },
    input: {
      flex: 1,
      minHeight: 44,
      borderRadius: radii.md,
      paddingHorizontal: spacing.md,
      color: palette.text,
      backgroundColor: palette.surfaceAlt,
      borderWidth: 1,
      borderColor: palette.border,
    },
    approvalTitle: { color: palette.text, fontSize: 14, fontWeight: "700" },
    footnote: { color: palette.textDim, fontSize: 11 },
  })
}
