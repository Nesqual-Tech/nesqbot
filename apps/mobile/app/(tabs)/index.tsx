/**
 * The inbox — the reason this app exists on a phone.
 *
 * The desktop app opens on a bot list, because on a desktop you are *working
 * with* the team. A phone is not that. The thing a person does with this
 * product away from their desk is unblock an agent that has stopped: approve a
 * spend, reject a send, or finish a login the bot cannot pass. So the first
 * screen is the queue of things waiting on them, and the bot list is one tab
 * over.
 *
 * Two kinds of block land here and they are kept visually distinct, because the
 * action differs:
 *
 *  - **Waiting for you at a screen** — a run parked in `awaiting_human`. The
 *    whole task is stopped, and there is no push notification for it (the API
 *    pushes approvals only), so this list is the only way it is ever seen.
 *    Listed first for that reason.
 *  - **Waiting for a decision** — a held action. Time-sensitive, but one step
 *    rather than a whole task, and a notification already chases it.
 */
import { useCallback, useMemo, useState } from "react"
import { StyleSheet, Text, View } from "react-native"
import { useFocusEffect, useRouter } from "expo-router"
import { productName } from "@nesqbot/ui"
import type { Approval, Bot } from "@nesqbot/protocol"
import { bots as botsApi } from "../../src/api/endpoints"
import { errorMessage } from "../../src/api/client"
import { useAuth } from "../../src/auth"
import { BotAvatar, Card, EmptyState, RiskBadge, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useFocusPolling } from "../../src/hooks/usePolling"
import { refreshInbox, useInbox } from "../../src/state/inbox"
import type { TakeoverRequest } from "../../src/lib/takeover"
import { useTheme, type Theme } from "../../src/theme"
import { relativeAge } from "../../src/utils/format"

/**
 * Faster than the old 30s badge poll, because this screen *is* the queue and a
 * stale inbox is the failure that matters. Still only while focused and in the
 * foreground — `useFocusPolling` stops on blur and on background.
 */
const POLL_MS = 12000

export default function InboxScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const router = useRouter()
  const { status, user } = useAuth()
  const authenticated = status === "authenticated"
  const inbox = useInbox()
  /**
   * Pull-to-refresh needs its own flag.
   *
   * The inbox lives in an external store, so there is no `useAsync` state to
   * borrow one from — and a `RefreshControl` given a constant `false` snaps back
   * the instant you let go, which reads as "nothing happened" on exactly the
   * gesture someone makes when they suspect the list is stale.
   */
  const [refreshing, setRefreshing] = useState(false)

  /**
   * Bots are fetched only for display names. A failure here must never hide the
   * queue, so it resolves to an empty map and the rows fall back to "Bot".
   */
  const botIndex = useAsync<Record<string, Bot>>(
    useCallback(
      (signal) =>
        botsApi
          .list({ signal })
          .then((list) => Object.fromEntries(list.map((bot) => [bot.id, bot])))
          .catch(() => ({})),
      [],
    ),
    [],
    { enabled: authenticated },
  )

  const load = useCallback(async () => {
    await refreshInbox()
  }, [])

  const pullToRefresh = useCallback(async () => {
    setRefreshing(true)
    try {
      await refreshInbox()
    } finally {
      setRefreshing(false)
    }
  }, [])

  useFocusEffect(
    useCallback(() => {
      if (authenticated) void load()
    }, [authenticated, load]),
  )
  useFocusPolling(load, POLL_MS, authenticated)

  const bots = botIndex.data ?? {}
  const botName = (id: string | null | undefined): string => (id && bots[id]?.name) || "Unknown bot"
  const { approvals, takeovers, count } = inbox

  const firstLoad = inbox.loadedAt === null && authenticated
  const bothFailed = inbox.approvalsError !== null && inbox.takeoversError !== null

  return (
    <Screen
      loading={firstLoad && !bothFailed}
      loadingLabel="Checking what needs you"
      error={firstLoad && bothFailed ? inbox.approvalsError : undefined}
      errorTitle="Could not reach the Nesq Bot API"
      onRetry={() => void load()}
      refreshing={refreshing}
      onRefresh={() => void pullToRefresh()}
      testID="inbox-screen"
    >
      <View style={styles.header}>
        <Text style={styles.eyebrow}>NESQUAL TECH</Text>
        <Text style={styles.title}>{productName}</Text>
        <Text style={styles.subtitle} accessibilityLiveRegion="polite">
          {summarise(count, takeovers.length, user?.display_name)}
        </Text>
      </View>

      {/* Partial failure. One half failing must not blank the other, so it is a
          line of text rather than the whole-screen error state. */}
      {!firstLoad && inbox.takeoversError ? (
        <Text style={styles.staleNotice}>Could not check for handovers: {errorMessage(inbox.takeoversError)}</Text>
      ) : null}
      {!firstLoad && inbox.approvalsError ? (
        <Text style={styles.staleNotice}>Could not refresh approvals: {errorMessage(inbox.approvalsError)}</Text>
      ) : null}

      {count === 0 && !firstLoad ? (
        <EmptyState
          title="Nothing is waiting"
          glyph="✓"
          message="Your bots are working. When one needs a decision or a person at the screen, it appears here — and an approval also arrives as a notification."
        />
      ) : null}

      {takeovers.length > 0 ? (
        <Text style={styles.sectionLabel} accessibilityRole="header">
          Waiting for you at a screen
        </Text>
      ) : null}
      {takeovers.map((request) => (
        <TakeoverRow
          key={request.runId}
          request={request}
          botName={request.botName ?? botName(request.botId)}
          botSlug={request.botId ? bots[request.botId]?.slug : undefined}
          styles={styles}
          onPress={() => router.push({ pathname: "/takeover/[runId]", params: { runId: request.runId } })}
        />
      ))}

      {approvals.length > 0 ? (
        <Text style={styles.sectionLabel} accessibilityRole="header">
          Waiting for a decision
        </Text>
      ) : null}
      {approvals.map((approval) => (
        <ApprovalRow
          key={approval.id}
          approval={approval}
          botName={botName(approval.bot_id)}
          botSlug={bots[approval.bot_id]?.slug}
          styles={styles}
          onPress={() => router.push({ pathname: "/approvals/[id]", params: { id: approval.id } })}
        />
      ))}
    </Screen>
  )
}

/** One sentence saying what is waiting, for sighted users and VoiceOver alike. */
function summarise(total: number, takeovers: number, displayName?: string | null): string {
  if (total === 0) return displayName ? `Nothing needs you, ${displayName.split(" ")[0]}.` : "Nothing needs you."
  const plural = total === 1 ? "thing needs" : "things need"
  if (takeovers === 0) return `${total} ${plural} your decision.`
  if (takeovers === total) return `${total} ${total === 1 ? "task is" : "tasks are"} waiting for you at a screen.`
  return `${total} ${plural} you, including ${takeovers} at a screen.`
}

/**
 * A parked run.
 *
 * Marked with the accent border rather than the danger colour: nothing has gone
 * wrong, the agent is asking for help. Reserving red for risk is what keeps a
 * `spend` approval legible at a glance.
 */
function TakeoverRow({
  request,
  botName,
  botSlug,
  styles,
  onPress,
}: {
  request: TakeoverRequest
  botName: string
  botSlug?: string
  styles: ReturnType<typeof makeStyles>
  onPress: () => void
}): JSX.Element {
  return (
    <Card
      onPress={onPress}
      style={styles.takeoverCard}
      accessibilityLabel={`${botName} needs you at the screen. ${request.reason}. ${request.whatYouNeed}. ${relativeAge(request.askedAt)}`}
      accessibilityHint="Opens the live desktop and the Continue button"
    >
      <View style={styles.row}>
        <Text style={styles.takeoverTag}>NEEDS YOU AT THE SCREEN</Text>
        <Text style={styles.age}>{relativeAge(request.askedAt)}</Text>
      </View>
      <Text style={styles.itemTitle}>{request.reason}</Text>
      <Text style={styles.summary} numberOfLines={3}>
        {request.whatYouNeed}
      </Text>
      <View style={styles.botRow}>
        <BotAvatar name={botName} slug={botSlug} size={20} />
        <Text style={styles.bot}>{botName}</Text>
        {request.resumeCount > 0 ? <Text style={styles.bot}>· resumed {request.resumeCount}× already</Text> : null}
      </View>
    </Card>
  )
}

function ApprovalRow({
  approval,
  botName,
  botSlug,
  styles,
  onPress,
}: {
  approval: Approval
  botName: string
  botSlug?: string
  styles: ReturnType<typeof makeStyles>
  onPress: () => void
}): JSX.Element {
  return (
    <Card
      onPress={onPress}
      accessibilityLabel={`${approval.title}. Risk ${approval.risk}. From ${botName}. ${relativeAge(approval.created_at)}`}
      accessibilityHint="Opens the approval so you can approve or reject it"
    >
      <View style={styles.row}>
        <RiskBadge risk={approval.risk} />
        <Text style={styles.age}>{relativeAge(approval.created_at)}</Text>
      </View>
      <Text style={styles.itemTitle}>{approval.title}</Text>
      {approval.summary ? (
        <Text style={styles.summary} numberOfLines={3}>
          {approval.summary}
        </Text>
      ) : null}
      <View style={styles.botRow}>
        <BotAvatar name={botName} slug={botSlug} size={20} />
        <Text style={styles.bot}>{botName}</Text>
      </View>
    </Card>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing, type } = theme
  return StyleSheet.create({
    header: { gap: 2, marginBottom: spacing.xs },
    /**
     * The one type-scale migration on this screen, and it is a deliberate one:
     * `type.eyebrow` is 11px at +1.8 tracking, measured off the Nesqual logo's
     * tagline, against the 11px/+2 this screen had hand-rolled. Same size, same
     * weight, tracking within a fifth of a pixel — and now it moves when the
     * brand does. The rest of this file's sizes stay literal until someone can
     * look at a simulator; see docs/mobile-parity.md.
     */
    eyebrow: { ...type.eyebrow, color: palette.textDim },
    title: { color: palette.text, fontSize: 30, fontWeight: "800" },
    subtitle: { color: palette.textMuted, fontSize: 14, lineHeight: 20 },
    sectionLabel: {
      ...type.labelCaps,
      color: palette.textDim,
      marginTop: spacing.sm,
    },
    row: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm },
    age: { color: palette.textDim, fontSize: 12 },
    itemTitle: { color: palette.text, fontWeight: "700", fontSize: 16, lineHeight: 22 },
    summary: { color: palette.textMuted, fontSize: 14, lineHeight: 20 },
    botRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    bot: { color: palette.textDim, fontSize: 12 },
    staleNotice: { color: palette.warning, fontSize: 12, lineHeight: 17 },
    takeoverCard: {
      borderColor: palette.accent,
      borderLeftWidth: 3,
      borderTopLeftRadius: radii.lg,
      borderBottomLeftRadius: radii.lg,
    },
    takeoverTag: { ...theme.type.labelCaps, color: palette.accent, flexShrink: 1 },
  })
}
