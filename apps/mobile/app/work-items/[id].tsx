/**
 * One work item, and the ledger of everyone who has held it.
 *
 * The ledger is the whole reason this screen exists. A competitor's agent hands
 * work to another agent and you get a timestamp; here every handover carries a
 * required reason, the bot it came from, the bot it went to, and whether a
 * human or a bot drove it. Reading it top to bottom answers "how did this end
 * up here", which is the question you ask right before you decide to move it
 * somewhere else.
 *
 * The one write kept on the phone is **transfer**, and it is gated behind a
 * reason box that cannot be empty — the API requires `min_length=1` and this
 * screen does not paper over that with a placeholder nobody chose. A ledger of
 * timestamps with no reasons is exactly what the differentiator is not.
 *
 * Editing title/summary/status/keys is absent on purpose: authoring belongs on
 * the desktop. Note also that `PATCH` refuses `owner_bot_id` with a 422 rather
 * than dropping it, precisely because `/transfer` is the only path that writes
 * the ledger — so there is no "quick reassign" shortcut to build here even if
 * one seemed convenient.
 */
import { useCallback, useMemo, useState } from "react"
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native"
import { Stack, useLocalSearchParams, useRouter } from "expo-router"
import type { Bot, WorkItem, WorkItemStatus, WorkItemTransfer } from "../../src/api/types"
import { bots as botsApi, workItems as workItemsApi } from "../../src/api/endpoints"
import { useAuth } from "../../src/auth"
import { Badge, BotAvatar, Button, Card, ErrorView, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useTheme, type Theme } from "../../src/theme"
import { formatDate, humanize, relativeAge } from "../../src/utils/format"

interface ItemData {
  item: WorkItem
  transfers: WorkItemTransfer[]
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

export default function WorkItemScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const router = useRouter()
  const { id } = useLocalSearchParams<{ id: string }>()
  const { status } = useAuth()
  const authenticated = status === "authenticated"

  const [transferring, setTransferring] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [targetBotId, setTargetBotId] = useState<string | null>(null)
  const [reason, setReason] = useState("")
  const [transferError, setTransferError] = useState<unknown>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(
    async (signal: AbortSignal): Promise<ItemData> => {
      const item = await workItemsApi.get(String(id), { signal })
      // The ledger and the bot names are context. Either failing must still
      // leave the item itself readable.
      const [transfers, botList] = await Promise.all([
        workItemsApi.transfers(item.id, { signal }).catch((): WorkItemTransfer[] => []),
        botsApi.list({ signal }).catch((): Bot[] => []),
      ])
      return { item, transfers, bots: Object.fromEntries(botList.map((bot) => [bot.id, bot])) }
    },
    [id],
  )

  const state = useAsync<ItemData>(load, [id], { enabled: authenticated && Boolean(id) })

  const item = state.data?.item ?? null
  const bots = state.data?.bots ?? {}
  const transfers = state.data?.transfers ?? []
  const closed = item?.status === "closed"

  const botName = (botId: string | null | undefined): string => (botId && bots[botId]?.name) || "a bot that is gone"

  const candidates = useMemo(
    () => Object.values(bots).filter((bot) => bot.id !== item?.owner_bot_id),
    [bots, item?.owner_bot_id],
  )

  const submitTransfer = useCallback(async () => {
    if (!item || !targetBotId || reason.trim().length === 0) return
    setTransferring(true)
    setTransferError(null)
    setNotice(null)
    try {
      const result = await workItemsApi.transfer(item.id, targetBotId, reason.trim())
      setSheetOpen(false)
      setReason("")
      setTargetBotId(null)
      // `transferred: false` is the idempotent answer — the target already held
      // it — and no ledger row was written. Saying "handed over" there would be
      // a lie about the one table this product asks to be trusted.
      setNotice(
        result.transferred
          ? `Handed to ${botName(result.work_item.owner_bot_id)}.`
          : `${botName(result.work_item.owner_bot_id)} already had this, so nothing changed.`,
      )
      void state.refresh()
    } catch (caught) {
      setTransferError(caught)
    } finally {
      setTransferring(false)
    }
  }, [item, targetBotId, reason, state, bots])

  return (
    <>
      {/* The item title is the first line of the card and is often long;
          repeating it in a 20pt-tall nav bar buys nothing and truncates badly. */}
      <Stack.Screen options={{ title: "Work item" }} />
      <Screen
        loading={state.loading}
        error={state.data === null ? (state.error ?? undefined) : undefined}
        errorTitle="Could not load this work item"
        onRetry={() => void state.reload()}
        refreshing={state.refreshing}
        onRefresh={() => void state.refresh()}
        testID="work-item-detail"
      >
        {item ? (
          <>
            <Card>
              <View style={styles.headerRow}>
                <Badge label={item.status.toUpperCase()} tone={statusTone(item.status)} />
                <Badge label={humanize(item.type).toUpperCase()} tone="neutral" />
              </View>
              <Text style={styles.title}>{item.title}</Text>
              {item.summary ? <Text style={styles.summary}>{item.summary}</Text> : null}
              <View style={styles.ownerRow}>
                {item.owner_bot_id && bots[item.owner_bot_id] ? (
                  <BotAvatar name={bots[item.owner_bot_id].name} slug={bots[item.owner_bot_id].slug} size={22} />
                ) : null}
                <Text style={styles.owner}>Held by {botName(item.owner_bot_id)}</Text>
              </View>
              <Text style={styles.meta}>
                Created {formatDate(item.created_at)}
                {item.last_event_at ? ` · last heard back ${relativeAge(item.last_event_at)}` : ""}
                {item.closed_at ? ` · closed ${relativeAge(item.closed_at)}` : ""}
              </Text>
              {item.resolution ? <Text style={styles.resolution}>{item.resolution}</Text> : null}
            </Card>

            {item.keys.length > 0 ? (
              <Card title="Recognised by" subtitle="How a reply from outside gets matched back to this item.">
                {item.keys.map((key) => (
                  <View key={`${key.channel}:${key.value}`} style={styles.keyRow}>
                    <Text style={styles.keyChannel}>{key.channel}</Text>
                    <Text style={styles.keyValue} selectable>
                      {key.value}
                    </Text>
                  </View>
                ))}
              </Card>
            ) : null}

            {notice ? <Text style={styles.notice}>{notice}</Text> : null}
            {transferError ? (
              <ErrorView
                error={transferError}
                compact
                title="The handover did not go through"
                onRetry={() => setTransferError(null)}
              />
            ) : null}

            <Card
              title="Handover ledger"
              subtitle={
                transfers.length === 0
                  ? "No rows — this item's history could not be read."
                  : `${transfers.length} ${transfers.length === 1 ? "row" : "rows"}, newest first.`
              }
            >
              {transfers.map((transfer, index) => (
                <View key={transfer.id} style={[styles.ledgerRow, index > 0 && styles.ledgerDivider]}>
                  <Text style={styles.ledgerWho}>
                    {/* Exactly one row has no `from_bot_id`: the opening
                        assignment written at creation, which is what makes the
                        ledger a complete answer rather than one missing its
                        first holder. */}
                    {transfer.from_bot_id
                      ? `${botName(transfer.from_bot_id)} → ${botName(transfer.to_bot_id)}`
                      : `Opened with ${botName(transfer.to_bot_id)}`}
                  </Text>
                  <Text style={styles.ledgerReason}>{transfer.reason}</Text>
                  <Text style={styles.ledgerMeta}>
                    {relativeAge(transfer.created_at)} ·{" "}
                    {transfer.actor_bot_id ? "driven by a bot" : "driven by a person"}
                    {transfer.source ? ` · ${transfer.source}` : ""}
                  </Text>
                </View>
              ))}
            </Card>

            {/* The ledger says who has held this; the thread says what they did
                about it. Reaching one from the other is the navigation worth
                having, and it is the only one this screen offers. */}
            {item.owner_bot_id ? (
              <Button
                label={`Open ${botName(item.owner_bot_id)}'s chat`}
                variant="ghost"
                block
                onPress={() => router.push({ pathname: "/chat/[botId]", params: { botId: String(item.owner_bot_id) } })}
                accessibilityLabel={`Open the conversation with ${botName(item.owner_bot_id)}`}
              />
            ) : null}

            {closed ? (
              <Text style={styles.meta}>
                This item is closed, so it cannot be handed over. Reopen it from the desktop app first.
              </Text>
            ) : (
              <Button
                label="Hand to another bot"
                variant="secondary"
                block
                onPress={() => setSheetOpen(true)}
                disabled={candidates.length === 0}
                accessibilityLabel="Hand this work item to another bot"
                accessibilityHint="Asks for a reason, which is recorded in the ledger"
              />
            )}
            {!closed && candidates.length === 0 ? (
              <Text style={styles.meta}>There is no other bot to hand this to.</Text>
            ) : null}
          </>
        ) : null}
      </Screen>

      <TransferSheet
        visible={sheetOpen}
        candidates={candidates}
        targetBotId={targetBotId}
        onPickBot={setTargetBotId}
        reason={reason}
        onChangeReason={setReason}
        submitting={transferring}
        onCancel={() => setSheetOpen(false)}
        onConfirm={() => void submitTransfer()}
        styles={styles}
        placeholderColor={theme.palette.textDim}
      />
    </>
  )
}

function TransferSheet({
  visible,
  candidates,
  targetBotId,
  onPickBot,
  reason,
  onChangeReason,
  submitting,
  onCancel,
  onConfirm,
  styles,
  placeholderColor,
}: {
  visible: boolean
  candidates: Bot[]
  targetBotId: string | null
  onPickBot: (id: string) => void
  reason: string
  onChangeReason: (value: string) => void
  submitting: boolean
  onCancel: () => void
  onConfirm: () => void
  styles: ReturnType<typeof makeStyles>
  placeholderColor: string
}): JSX.Element {
  const ready = targetBotId !== null && reason.trim().length > 0

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onCancel} accessibilityViewIsModal>
      <KeyboardAvoidingView style={styles.sheetBackdrop} behavior={Platform.OS === "ios" ? "padding" : undefined}>
        <Pressable
          style={styles.sheetScrim}
          onPress={onCancel}
          accessibilityRole="button"
          accessibilityLabel="Dismiss without handing over"
        />
        <View style={styles.sheet}>
          <View style={styles.sheetHandle} />
          <Text style={styles.sheetTitle}>Hand this to</Text>

          <ScrollView style={styles.botList} keyboardShouldPersistTaps="handled">
            {candidates.map((bot) => {
              const selected = bot.id === targetBotId
              return (
                <Pressable
                  key={bot.id}
                  onPress={() => onPickBot(bot.id)}
                  accessibilityRole="radio"
                  accessibilityState={{ selected }}
                  accessibilityLabel={`${bot.name}. ${bot.role}`}
                  style={({ pressed }) => [
                    styles.botOption,
                    selected && styles.botOptionSelected,
                    pressed && styles.botOptionPressed,
                  ]}
                >
                  <BotAvatar name={bot.name} slug={bot.slug} size={26} />
                  <View style={styles.botOptionText}>
                    <Text style={styles.botOptionName}>{bot.name}</Text>
                    <Text style={styles.botOptionRole} numberOfLines={1}>
                      {bot.role}
                    </Text>
                  </View>
                  {selected ? <Text style={styles.tick}>✓</Text> : null}
                </Pressable>
              )
            })}
          </ScrollView>

          <Text style={styles.sheetLabel}>Why</Text>
          <TextInput
            style={styles.input}
            value={reason}
            onChangeText={onChangeReason}
            placeholder="e.g. qualified and ready to close"
            placeholderTextColor={placeholderColor}
            multiline
            maxLength={500}
            accessibilityLabel="Reason for the handover"
            accessibilityHint="Required. It is written into the ledger and cannot be changed later."
          />
          <Text style={styles.sheetHint}>
            Required, and permanent. The ledger keeps this row even if the item is later deleted.
          </Text>

          <View style={styles.sheetActions}>
            <Button
              label="Hand over"
              onPress={onConfirm}
              disabled={!ready || submitting}
              loading={submitting}
              accessibilityLabel="Confirm the handover"
              style={styles.sheetAction}
            />
            <Button
              label="Cancel"
              variant="ghost"
              onPress={onCancel}
              accessibilityLabel="Cancel the handover"
              style={styles.sheetAction}
            />
          </View>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing, type } = theme
  return StyleSheet.create({
    headerRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    title: { color: palette.text, fontWeight: "800", fontSize: 19, lineHeight: 25 },
    summary: { color: palette.textMuted, fontSize: 14, lineHeight: 20 },
    ownerRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    owner: { color: palette.text, fontSize: 14, fontWeight: "600" },
    meta: { color: palette.textDim, fontSize: 12, lineHeight: 17 },
    resolution: { color: palette.success, fontSize: 14, lineHeight: 20 },
    notice: { color: palette.success, fontSize: 13 },
    keyRow: { flexDirection: "row", alignItems: "baseline", gap: spacing.sm },
    keyChannel: { ...type.labelCaps, color: palette.textDim, minWidth: 72 },
    keyValue: { color: palette.text, fontSize: 14, flexShrink: 1 },
    ledgerRow: { gap: 2, paddingVertical: spacing.sm },
    ledgerDivider: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: palette.border },
    ledgerWho: { color: palette.text, fontSize: 14, fontWeight: "700" },
    ledgerReason: { color: palette.textMuted, fontSize: 14, lineHeight: 20 },
    ledgerMeta: { color: palette.textDim, fontSize: 11 },
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
    sheetLabel: { ...type.labelCaps, color: palette.textDim, marginTop: spacing.sm },
    sheetHint: { color: palette.textDim, fontSize: 12, lineHeight: 17 },
    botList: { maxHeight: 220 },
    botOption: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.md,
      minHeight: 52,
      paddingHorizontal: spacing.md,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: "transparent",
    },
    botOptionSelected: { borderColor: palette.accent, backgroundColor: palette.surfaceAlt },
    botOptionPressed: { backgroundColor: palette.surfacePressed },
    botOptionText: { flex: 1, gap: 1 },
    botOptionName: { color: palette.text, fontSize: 15, fontWeight: "600" },
    botOptionRole: { color: palette.textDim, fontSize: 12 },
    tick: { color: palette.accent, fontSize: 16, fontWeight: "700" },
    input: {
      minHeight: 72,
      maxHeight: 140,
      color: palette.text,
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
      padding: spacing.md,
      textAlignVertical: "top",
    },
    sheetActions: { flexDirection: "row", gap: spacing.sm, marginTop: spacing.xs },
    sheetAction: { flex: 1 },
  })
}
