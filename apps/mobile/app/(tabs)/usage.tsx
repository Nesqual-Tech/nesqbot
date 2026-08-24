import { useCallback, useMemo, useState } from "react"
import { Modal, Pressable, StyleSheet, Text, View, type DimensionValue } from "react-native"
import type { UsageOut } from "../../src/api/types"
import { bots as botsApi, usage as usageApi } from "../../src/api/endpoints"
import { useAuth } from "../../src/auth"
import { Badge, Button, Card, EmptyState, ErrorView, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useTheme, type Theme } from "../../src/theme"
import { clampPercent, formatUsd } from "../../src/utils/format"

const RANGES: { label: string; days: number }[] = [
  { label: "Today", days: 1 },
  { label: "7 days", days: 7 },
  { label: "30 days", days: 30 },
]

/**
 * Cap raises offered as fixed steps rather than a text field.
 *
 * This is a number typed under time pressure on a phone keyboard, on a screen
 * where a slipped decimal point is a real bill. Presets remove the class of
 * mistake entirely, and the two shapes people actually want — "a bit more to
 * finish today" and "clearly double it" — are both here.
 */
const CAP_STEPS: { label: string; apply: (current: number) => number }[] = [
  { label: "+$5", apply: (current) => current + 5 },
  { label: "+$25", apply: (current) => current + 25 },
  { label: "Double", apply: (current) => Math.max(1, current * 2) },
]

interface CapTarget {
  botId: string
  botName: string
  current: number
  spent: number
}

export default function UsageScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const { status } = useAuth()
  const authenticated = status === "authenticated"
  const [days, setDays] = useState(1)
  const [capTarget, setCapTarget] = useState<CapTarget | null>(null)
  const [capBusy, setCapBusy] = useState(false)
  const [capError, setCapError] = useState<unknown>(null)
  const [capNotice, setCapNotice] = useState<string | null>(null)

  const load = useCallback((signal: AbortSignal): Promise<UsageOut[]> => usageApi.list(days, { signal }), [days])
  const state = useAsync<UsageOut[]>(load, [days], { enabled: authenticated })

  /**
   * Raise one bot's daily cap.
   *
   * The honest part is what this does **not** do: the turn that was already
   * refused is gone, not queued. Raising the cap lets the *next* one run. Saying
   * so is the difference between a person waiting for a reply that will never
   * arrive and one who asks again.
   */
  const raiseCap = useCallback(
    async (next: number) => {
      if (!capTarget) return
      setCapBusy(true)
      setCapError(null)
      try {
        await botsApi.setBudget(capTarget.botId, Number(next.toFixed(2)))
        setCapNotice(`${capTarget.botName} can spend up to ${formatUsd(next)} a day now. Ask it again to carry on.`)
        setCapTarget(null)
        void state.refresh()
      } catch (caught) {
        setCapError(caught)
      } finally {
        setCapBusy(false)
      }
    },
    [capTarget, state],
  )

  const rows = state.data ?? []
  const totalSpend = rows.reduce((sum, row) => sum + (row.spent_usd_today || 0), 0)
  const totalBudget = rows.reduce((sum, row) => sum + (row.budget_usd || 0), 0)

  return (
    <Screen
      loading={state.loading}
      error={state.data === null ? (state.error ?? undefined) : undefined}
      errorTitle="Could not load usage"
      onRetry={() => void state.reload()}
      refreshing={state.refreshing}
      onRefresh={() => void state.refresh()}
      testID="usage-screen"
    >
      <View style={styles.rangeRow} accessibilityRole="tablist">
        {RANGES.map((range) => {
          const active = range.days === days
          return (
            <Pressable
              key={range.days}
              onPress={() => setDays(range.days)}
              accessibilityRole="tab"
              accessibilityState={{ selected: active }}
              accessibilityLabel={`Show usage for ${range.label}`}
              style={[styles.rangeChip, active && styles.rangeChipActive]}
            >
              <Text style={[styles.rangeText, active && styles.rangeTextActive]}>{range.label}</Text>
            </Pressable>
          )
        })}
      </View>

      <Card title="Total" subtitle={`${rows.length} bot${rows.length === 1 ? "" : "s"}`}>
        <Text style={styles.total}>{formatUsd(totalSpend)}</Text>
        <Text style={styles.meta}>of {formatUsd(totalBudget)} budgeted per day</Text>
        <Meter value={clampPercent(totalBudget > 0 ? totalSpend / totalBudget : 0)} styles={styles} />
      </Card>

      {capNotice ? (
        <Text style={styles.notice} accessibilityLiveRegion="polite">
          {capNotice}
        </Text>
      ) : null}
      {capError ? (
        <ErrorView error={capError} compact title="Could not change the cap" onRetry={() => setCapError(null)} />
      ) : null}

      {rows.length === 0 && !state.loading ? (
        <EmptyState
          title="No spend recorded"
          glyph="◑"
          message="Cost shows up here as soon as your bots start running turns."
        />
      ) : null}

      {rows.map((row) => {
        const ratio = row.budget_usd > 0 ? row.spent_usd_today / row.budget_usd : 0
        const over = ratio >= 1
        const near = !over && ratio >= 0.8
        return (
          <Card
            key={row.bot_id}
            title={row.bot_name}
            right={
              over ? (
                <Badge label="OVER BUDGET" tone="danger" />
              ) : near ? (
                <Badge label="NEAR CAP" tone="warning" />
              ) : null
            }
          >
            <View style={styles.spendRow}>
              <Text style={styles.spend}>{formatUsd(row.spent_usd_today)}</Text>
              <Text style={styles.meta}>/ {formatUsd(row.budget_usd)}</Text>
            </View>
            <Meter value={clampPercent(ratio)} danger={over} warning={near} styles={styles} />
            {over ? (
              <Text style={styles.blocked}>
                At the cap this bot stops calling models: turns come back budget-blocked with no reply until the cap is
                raised or the day rolls over.
              </Text>
            ) : null}
            {/* A bot stuck at its cap is a stopped agent, which is the one thing
                this app exists to restart. One number, one tap, from wherever
                you happen to be. */}
            {over || near ? (
              <Button
                label="Raise the cap"
                size="sm"
                variant={over ? "primary" : "ghost"}
                onPress={() =>
                  setCapTarget({
                    botId: row.bot_id,
                    botName: row.bot_name,
                    current: row.budget_usd,
                    spent: row.spent_usd_today,
                  })
                }
                accessibilityLabel={`Raise the daily cap for ${row.bot_name}, currently ${formatUsd(row.budget_usd)}`}
              />
            ) : null}
            <Text style={styles.meta}>
              {row.entries.length} ledger entr{row.entries.length === 1 ? "y" : "ies"}
            </Text>
          </Card>
        )
      })}

      <Modal
        visible={capTarget !== null}
        transparent
        animationType="slide"
        onRequestClose={() => setCapTarget(null)}
        accessibilityViewIsModal
      >
        <View style={styles.sheetBackdrop}>
          <Pressable
            style={styles.sheetScrim}
            onPress={() => setCapTarget(null)}
            accessibilityRole="button"
            accessibilityLabel="Dismiss without changing the cap"
          />
          <View style={styles.sheet}>
            <View style={styles.sheetHandle} />
            <Text style={styles.sheetTitle}>Raise {capTarget?.botName ?? "the"} cap</Text>
            <Text style={styles.meta}>
              Spent {formatUsd(capTarget?.spent ?? 0)} of {formatUsd(capTarget?.current ?? 0)} today.
            </Text>
            <View style={styles.sheetActions}>
              {CAP_STEPS.map((step) => {
                const next = step.apply(capTarget?.current ?? 0)
                return (
                  <Button
                    key={step.label}
                    label={step.label}
                    size="sm"
                    variant="secondary"
                    disabled={capBusy}
                    onPress={() => void raiseCap(next)}
                    accessibilityLabel={`Raise the cap to ${formatUsd(next)} a day`}
                    style={styles.sheetAction}
                  />
                )
              })}
            </View>
            <Text style={styles.sheetHint}>
              The turn that was already refused is not retried — raising the cap lets the next one run, so ask the bot
              again once you have.
            </Text>
            <Button
              label="Cancel"
              variant="ghost"
              onPress={() => setCapTarget(null)}
              accessibilityLabel="Leave the cap as it is"
            />
          </View>
        </View>
      </Modal>
    </Screen>
  )
}

function Meter({
  value,
  danger = false,
  warning = false,
  styles,
}: {
  value: number
  danger?: boolean
  warning?: boolean
  styles: ReturnType<typeof makeStyles>
}): JSX.Element {
  const percent = Math.round(value * 100)
  const width = `${Math.max(2, percent)}%` as DimensionValue
  return (
    <View
      style={styles.meterTrack}
      accessibilityRole="progressbar"
      accessibilityValue={{ min: 0, max: 100, now: percent }}
      accessibilityLabel={`${percent}% of budget used`}
    >
      <View style={[styles.meterFill, danger && styles.meterDanger, warning && styles.meterWarning, { width }]} />
    </View>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    rangeRow: { flexDirection: "row", gap: spacing.sm },
    rangeChip: {
      minHeight: 36,
      justifyContent: "center",
      paddingHorizontal: spacing.md,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: palette.border,
      backgroundColor: palette.surface,
    },
    rangeChipActive: { backgroundColor: palette.accent, borderColor: palette.accent },
    rangeText: { color: palette.textMuted, fontSize: 13, fontWeight: "600" },
    rangeTextActive: { color: theme.onAccent },
    total: { color: palette.text, fontSize: 28, fontWeight: "800" },
    spendRow: { flexDirection: "row", alignItems: "baseline", gap: spacing.xs },
    spend: { color: palette.text, fontSize: 20, fontWeight: "700" },
    meta: { color: palette.textMuted, fontSize: 12 },
    blocked: { color: palette.danger, fontSize: 12, lineHeight: 17 },
    meterTrack: {
      height: 8,
      borderRadius: 4,
      backgroundColor: palette.surfaceAlt,
      overflow: "hidden",
    },
    meterFill: { height: 8, borderRadius: 4, backgroundColor: palette.accent },
    meterWarning: { backgroundColor: palette.warning },
    meterDanger: { backgroundColor: palette.danger },
    notice: { color: palette.success, fontSize: 13, lineHeight: 19 },
    sheetBackdrop: { flex: 1, justifyContent: "flex-end" },
    sheetScrim: { ...StyleSheet.absoluteFillObject, backgroundColor: palette.bg, opacity: 0.75 },
    sheet: {
      backgroundColor: palette.surface,
      borderTopLeftRadius: radii.xl,
      borderTopRightRadius: radii.xl,
      borderTopWidth: 1,
      borderColor: palette.border,
      padding: spacing.xl,
      paddingBottom: spacing.xxl,
      gap: spacing.sm,
    },
    sheetHandle: {
      alignSelf: "center",
      width: 40,
      height: 4,
      borderRadius: 2,
      backgroundColor: palette.border,
      marginBottom: spacing.sm,
    },
    sheetTitle: { color: palette.text, fontWeight: "800", fontSize: 18 },
    sheetHint: { color: palette.textDim, fontSize: 12, lineHeight: 17 },
    sheetActions: { flexDirection: "row", gap: spacing.sm, marginTop: spacing.xs },
    sheetAction: { flex: 1 },
  })
}
