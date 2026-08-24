/**
 * Work items — what each bot is actually holding.
 *
 * A work item is the owned, transferable unit of work a bot hands to another
 * bot, and every handover is a row in a ledger with a required reason. That
 * ledger is the product's stated differentiator, so "which of my agents is
 * sitting on what, and how long has it been sitting there" is worth a tab — it
 * is a glance, which is a phone's best shape.
 *
 * What is deliberately **not** here: creating items and editing their fields.
 * That is authoring — a title, a summary, external keys, a type — and it is a
 * bad phone job and a good desktop one. The one write this screen's detail view
 * keeps is `transfer`, because handing a stalled item to a different bot is a
 * decision, and decisions are what this app is for.
 */
import { useCallback, useMemo, useState } from "react"
import { Pressable, StyleSheet, Text, View } from "react-native"
import { useFocusEffect, useRouter } from "expo-router"
import type { Bot, WorkItem, WorkItemStatus } from "../../src/api/types"
import { bots as botsApi, workItems as workItemsApi } from "../../src/api/endpoints"
import { useAuth } from "../../src/auth"
import { Badge, BotAvatar, Card, EmptyState, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useTheme, type Theme } from "../../src/theme"
import { humanize, relativeAge } from "../../src/utils/format"

/**
 * The filters, in the order a person actually asks the question.
 *
 * `waiting` is second rather than last because it is the one that rots: an item
 * blocked on the outside world with a `last_event_at` from four days ago is a
 * lead going cold, and that is the thing worth noticing from a phone.
 */
const FILTERS: { key: "live" | WorkItemStatus; label: string }[] = [
  { key: "live", label: "Live" },
  { key: "waiting", label: "Waiting" },
  { key: "working", label: "Working" },
  { key: "open", label: "Open" },
  { key: "closed", label: "Closed" },
]

interface WorkData {
  items: WorkItem[]
  bots: Record<string, Bot>
}

function statusTone(status: WorkItemStatus): "neutral" | "accent" | "warning" | "success" {
  switch (status) {
    case "working":
      return "accent"
    case "waiting":
      return "warning"
    case "closed":
      return "success"
    default:
      return "neutral"
  }
}

export default function WorkScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const router = useRouter()
  const { status } = useAuth()
  const authenticated = status === "authenticated"
  const [filter, setFilter] = useState<"live" | WorkItemStatus>("live")

  const load = useCallback(
    async (signal: AbortSignal): Promise<WorkData> => {
      // "Live" is not a server-side status — it is everything not closed — so it
      // is fetched unfiltered and narrowed here rather than by three round trips.
      const query = filter === "live" ? { limit: 100 } : { status: filter, limit: 100 }
      const [items, botList] = await Promise.all([
        workItemsApi.list(query, { signal }),
        botsApi.list({ signal }).catch((): Bot[] => []),
      ])
      return {
        items: filter === "live" ? items.filter((item) => item.status !== "closed") : items,
        bots: Object.fromEntries(botList.map((bot) => [bot.id, bot])),
      }
    },
    [filter],
  )

  const state = useAsync<WorkData>(load, [filter], { enabled: authenticated })

  useFocusEffect(
    useCallback(() => {
      if (authenticated) void state.refresh()
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [authenticated]),
  )

  const items = state.data?.items ?? []
  const bots = state.data?.bots ?? {}

  return (
    <Screen
      loading={state.loading}
      error={state.data === null ? (state.error ?? undefined) : undefined}
      errorTitle="Could not load work items"
      onRetry={() => void state.reload()}
      refreshing={state.refreshing}
      onRefresh={() => void state.refresh()}
      testID="work-screen"
    >
      <View style={styles.filters} accessibilityRole="tablist">
        {FILTERS.map((option) => {
          const active = option.key === filter
          return (
            <Pressable
              key={option.key}
              onPress={() => setFilter(option.key)}
              accessibilityRole="tab"
              accessibilityState={{ selected: active }}
              accessibilityLabel={`Show ${option.label.toLowerCase()} work items`}
              hitSlop={6}
              style={({ pressed }) => [styles.filter, active && styles.filterActive, pressed && styles.filterPressed]}
            >
              <Text style={[styles.filterLabel, active && styles.filterLabelActive]}>{option.label}</Text>
            </Pressable>
          )
        })}
      </View>

      {state.error && state.data !== null ? (
        <Text style={styles.staleNotice}>Showing the last known list — refresh failed.</Text>
      ) : null}

      {items.length === 0 && !state.loading ? (
        <EmptyState
          title={filter === "closed" ? "Nothing closed yet" : "No work in flight"}
          glyph="≡"
          message="Work items appear when a bot picks up something it owns — a lead, a ticket, an invoice — and every handover between bots is recorded here."
        />
      ) : null}

      {items.map((item) => {
        const owner = item.owner_bot_id ? bots[item.owner_bot_id] : undefined
        return (
          <Card
            key={item.id}
            onPress={() => router.push({ pathname: "/work-items/[id]", params: { id: item.id } })}
            accessibilityLabel={`${item.title}. ${item.status}. Held by ${owner?.name ?? "no bot"}. ${lastTouched(item)}`}
            accessibilityHint="Opens the item and its handover ledger"
          >
            <View style={styles.row}>
              <Badge label={item.status.toUpperCase()} tone={statusTone(item.status)} />
              <Text style={styles.age}>{lastTouched(item)}</Text>
            </View>
            <Text style={styles.itemTitle}>{item.title}</Text>
            {item.summary ? (
              <Text style={styles.summary} numberOfLines={2}>
                {item.summary}
              </Text>
            ) : null}
            <View style={styles.botRow}>
              {owner ? <BotAvatar name={owner.name} slug={owner.slug} size={20} /> : null}
              <Text style={styles.bot}>
                {owner ? owner.name : "Unassigned"} · {humanize(item.type)}
              </Text>
            </View>
          </Card>
        )
      })}
    </Screen>
  )
}

/**
 * "Replied 2h ago" versus "Updated 2h ago" — and the distinction is the point.
 *
 * `last_event_at` moves only when the **outside world** acted; `updated_at`
 * moves on any edit. An item whose last real event was four days ago is stalled
 * whatever its `updated_at` says, and collapsing the two would hide exactly the
 * rows worth chasing.
 */
function lastTouched(item: WorkItem): string {
  if (item.last_event_at) return `heard back ${relativeAge(item.last_event_at)}`
  return `updated ${relativeAge(item.updated_at)}`
}

function makeStyles(theme: Theme) {
  const { palette, radiusPill, spacing, type } = theme
  return StyleSheet.create({
    filters: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm },
    filter: {
      // 32pt tall plus the 6pt hitSlop above clears the 44pt touch target
      // without a chip row that eats a third of the screen.
      minHeight: 32,
      justifyContent: "center",
      paddingHorizontal: spacing.md,
      borderRadius: radiusPill,
      borderWidth: 1,
      borderColor: palette.border,
      backgroundColor: palette.surface,
    },
    filterActive: { backgroundColor: palette.surfaceRaised, borderColor: palette.accent },
    filterPressed: { backgroundColor: palette.surfacePressed },
    filterLabel: { ...type.label, color: palette.textMuted },
    filterLabelActive: { color: palette.text },
    row: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm },
    age: { color: palette.textDim, fontSize: 12, flexShrink: 1, textAlign: "right" },
    itemTitle: { color: palette.text, fontWeight: "700", fontSize: 16, lineHeight: 21 },
    summary: { color: palette.textMuted, fontSize: 14, lineHeight: 20 },
    botRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    bot: { color: palette.textDim, fontSize: 12, flexShrink: 1 },
    staleNotice: { color: palette.warning, fontSize: 12 },
  })
}
