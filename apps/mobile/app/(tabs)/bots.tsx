/**
 * The team.
 *
 * Was the app's home screen; it is now one tab over, because a phone opens on
 * what is *waiting* rather than on a roster (see `index.tsx`). What it keeps is
 * everything a person needs to give a bot context before deciding something:
 * who it is, what it costs, and the two ways to look at it — the conversation
 * and the screen it is working on.
 *
 * Creating and editing bots is not here on purpose. That is authoring — long
 * forms, a system prompt, connector bindings — and it lives on the desktop.
 */
import { useCallback, useMemo } from "react"
import { StyleSheet, Text, View } from "react-native"
import { useRouter } from "expo-router"
import type { Bot } from "@nesqbot/protocol"
import { bots as botsApi } from "../../src/api/endpoints"
import { useAuth } from "../../src/auth"
import { Badge, BotAvatar, Button, Card, EmptyState, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useInbox } from "../../src/state/inbox"
import { useTheme, type Theme } from "../../src/theme"
import { formatUsd, humanize } from "../../src/utils/format"

export default function BotsScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const router = useRouter()
  const { status } = useAuth()
  const authenticated = status === "authenticated"
  const { approvals, takeovers } = useInbox()

  const bots = useAsync<Bot[]>(
    useCallback((signal) => botsApi.list({ signal }), []),
    [],
    { enabled: authenticated },
  )

  const list = bots.data ?? []

  /**
   * How many things each bot is blocked on. Counted from the inbox the badge
   * already holds rather than re-fetched, so the roster and the queue can never
   * disagree with each other.
   */
  const blockedBy = useMemo(() => {
    const counts = new Map<string, number>()
    for (const approval of approvals) counts.set(approval.bot_id, (counts.get(approval.bot_id) ?? 0) + 1)
    for (const takeover of takeovers) {
      if (takeover.botId) counts.set(takeover.botId, (counts.get(takeover.botId) ?? 0) + 1)
    }
    return counts
  }, [approvals, takeovers])

  return (
    <Screen
      loading={bots.loading}
      error={list.length === 0 ? (bots.error ?? undefined) : undefined}
      errorTitle="Could not load your bots"
      onRetry={() => void bots.reload()}
      refreshing={bots.refreshing}
      onRefresh={() => void bots.refresh()}
      testID="bots-screen"
    >
      {bots.error && list.length > 0 ? (
        <Text style={styles.staleNotice}>Showing cached bots — the last refresh failed.</Text>
      ) : null}

      {list.length === 0 && !bots.loading && !bots.error ? (
        <EmptyState
          title="No bots yet"
          glyph="◉"
          message="Create a bot from the desktop app and it will show up here."
          actionLabel="Refresh"
          onAction={() => void bots.reload()}
        />
      ) : null}

      {list.map((bot) => {
        const blocked = blockedBy.get(bot.id) ?? 0
        return (
          <Card key={bot.id} style={styles.botCard}>
            <View style={styles.botRow}>
              <BotAvatar name={bot.name} slug={bot.slug} />
              <View style={styles.botText}>
                <Text style={styles.botName}>{bot.name}</Text>
                <Text style={styles.botRole} numberOfLines={2}>
                  {bot.role}
                </Text>
              </View>
              {bot.is_system ? <Badge label="SYSTEM" tone="neutral" /> : null}
            </View>

            {blocked > 0 ? (
              <Text
                style={styles.blocked}
                accessibilityLabel={`${bot.name} is blocked on ${blocked} ${blocked === 1 ? "thing" : "things"}. Open the Inbox to clear it.`}
              >
                Blocked on {blocked} {blocked === 1 ? "thing" : "things"} — see Inbox
              </Text>
            ) : null}

            <Text style={styles.metaText}>
              Budget {formatUsd(bot.daily_budget_usd)} / day · {humanize(bot.desktop_profile)} desktop
            </Text>

            <View style={styles.actions}>
              <Button
                label="Chat"
                size="sm"
                onPress={() => router.push({ pathname: "/chat/[botId]", params: { botId: bot.id } })}
                accessibilityLabel={`Chat with ${bot.name}`}
                style={styles.action}
              />
              <Button
                label="Desktop"
                size="sm"
                variant="ghost"
                onPress={() => router.push({ pathname: "/desktop/[botId]", params: { botId: bot.id } })}
                accessibilityLabel={`Watch the Bot Desktop for ${bot.name}`}
                style={styles.action}
              />
            </View>
          </Card>
        )
      })}
    </Screen>
  )
}

function makeStyles(theme: Theme) {
  const { palette, spacing } = theme
  return StyleSheet.create({
    staleNotice: { color: palette.warning, fontSize: 12 },
    botCard: { gap: spacing.md },
    botRow: { flexDirection: "row", alignItems: "center", gap: spacing.md },
    botText: { flex: 1, gap: 2 },
    botName: { color: palette.text, fontWeight: "700", fontSize: 16 },
    botRole: { color: palette.textMuted, fontSize: 13 },
    metaText: { color: palette.textDim, fontSize: 12 },
    blocked: { color: palette.accent, fontSize: 13, fontWeight: "700" },
    actions: { flexDirection: "row", gap: spacing.sm },
    action: { flex: 1 },
  })
}
