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
import { useCallback, useMemo, useState } from "react"
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native"
import { useRouter } from "expo-router"
import type { Bot, ModelProvider, ProvidersOut } from "@nesqbot/protocol"
import { errorMessage } from "../../src/api/client"
import { bots as botsApi } from "../../src/api/endpoints"
import { useAuth } from "../../src/auth"
import { Badge, BotAvatar, Button, Card, EmptyState, Screen } from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { useInbox } from "../../src/state/inbox"
import { useTheme, type Theme } from "../../src/theme"
import { formatUsd, humanize } from "../../src/utils/format"

const PROVIDER_LABEL: Record<ModelProvider, string> = {
  azure: "Azure OpenAI",
  openai: "OpenAI / local",
  anthropic: "Anthropic",
  google: "Google",
}

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

  // Which providers this deployment can actually reach right now - drives the
  // per-bot model picker below. Not fatal if it fails to load: the picker
  // just has nothing to offer, same as a deployment with no provider
  // configured at all.
  const providers = useAsync<ProvidersOut>(
    useCallback((signal) => botsApi.providers({ signal }), []),
    [],
    { enabled: authenticated },
  )
  const availableProviders = useMemo(
    () =>
      providers.data
        ? (Object.keys(providers.data) as ModelProvider[]).filter((key) => providers.data![key])
        : [],
    [providers.data],
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

            <BotModelSection
              bot={bot}
              availableProviders={availableProviders}
              onUpdated={(updated) => bots.setData((previous) => (previous ?? []).map((b) => (b.id === updated.id ? updated : b)))}
            />

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

/**
 * Which provider/model this bot is pinned to, and the control to change it.
 *
 * Deliberately not a general edit form — this file's header comment is clear
 * that authoring (name, role, system prompt, connectors) lives on the
 * desktop, on purpose. A provider/model pin is closer to the budget field
 * two rows up than to authoring: one operational setting, not a form, so it
 * stays here rather than growing bots.tsx into the thing this screen exists
 * to not be.
 *
 * Collapsed by default to a single status line; "Change" expands the picker
 * inline rather than navigating away, since there is nothing else on this
 * setting worth a dedicated screen.
 */
function BotModelSection({
  bot,
  availableProviders,
  onUpdated,
}: {
  bot: Bot
  availableProviders: ModelProvider[]
  onUpdated: (bot: Bot) => void
}): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeModelStyles(theme), [theme])
  const [editing, setEditing] = useState(false)
  const [provider, setProvider] = useState<ModelProvider | null>(bot.model_provider)
  const [modelName, setModelName] = useState(bot.model_name ?? "")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const startEditing = useCallback(() => {
    setProvider(bot.model_provider)
    setModelName(bot.model_name ?? "")
    setError(null)
    setEditing(true)
  }, [bot.model_provider, bot.model_name])

  const canSave = provider ? modelName.trim().length > 0 : true

  const save = useCallback(async () => {
    if (!canSave) return
    setSaving(true)
    setError(null)
    try {
      const updated = await botsApi.setModel(bot.id, {
        model_provider: provider,
        model_name: provider ? modelName.trim() : null,
      })
      onUpdated(updated)
      setEditing(false)
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setSaving(false)
    }
  }, [bot.id, provider, modelName, canSave, onUpdated])

  if (!editing) {
    return (
      <Pressable
        onPress={startEditing}
        accessibilityRole="button"
        accessibilityLabel={`Change the model ${bot.name} uses`}
        style={styles.summaryRow}
      >
        <Text style={styles.summaryText}>
          Model: {bot.model_provider ? `${PROVIDER_LABEL[bot.model_provider]} · ${bot.model_name}` : "Default (tier routing)"}
        </Text>
        <Text style={styles.change}>Change</Text>
      </Pressable>
    )
  }

  return (
    <View style={styles.editor}>
      <View style={styles.chipsRow}>
        <Pressable
          onPress={() => setProvider(null)}
          accessibilityRole="radio"
          accessibilityState={{ selected: provider === null, checked: provider === null }}
          style={[styles.chip, provider === null && styles.chipActive]}
        >
          <Text style={[styles.chipText, provider === null && styles.chipTextActive]}>Default</Text>
        </Pressable>
        {availableProviders.map((key) => {
          const active = provider === key
          return (
            <Pressable
              key={key}
              onPress={() => setProvider(key)}
              accessibilityRole="radio"
              accessibilityState={{ selected: active, checked: active }}
              style={[styles.chip, active && styles.chipActive]}
            >
              <Text style={[styles.chipText, active && styles.chipTextActive]}>{PROVIDER_LABEL[key]}</Text>
            </Pressable>
          )
        })}
      </View>

      {provider ? (
        <TextInput
          style={styles.input}
          value={modelName}
          onChangeText={setModelName}
          placeholder="model name, e.g. claude-opus-5"
          placeholderTextColor={theme.palette.textDim}
          autoCapitalize="none"
          autoCorrect={false}
          accessibilityLabel={`Model name for ${bot.name}`}
        />
      ) : (
        <Text style={styles.hint}>Follows the router's ordinary tier routing.</Text>
      )}

      {error ? <Text style={styles.error}>{error}</Text> : null}

      <View style={styles.editorActions}>
        <Button
          label="Save"
          size="sm"
          onPress={() => void save()}
          loading={saving}
          disabled={!canSave}
          style={styles.editorAction}
          accessibilityLabel={`Save the model for ${bot.name}`}
        />
        <Button
          label="Cancel"
          size="sm"
          variant="ghost"
          onPress={() => setEditing(false)}
          disabled={saving}
          style={styles.editorAction}
        />
      </View>
    </View>
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

function makeModelStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    summaryRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
    summaryText: { color: palette.textMuted, fontSize: 12, flex: 1 },
    change: { color: palette.accent, fontSize: 12, fontWeight: "700" },
    editor: { gap: spacing.sm },
    chipsRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
    chip: {
      paddingHorizontal: 10,
      paddingVertical: 6,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: palette.border,
      backgroundColor: palette.surfaceRaised,
    },
    chipActive: { backgroundColor: palette.accent, borderColor: palette.accent },
    chipText: { color: palette.textMuted, fontWeight: "600", fontSize: 12 },
    chipTextActive: { color: theme.onAccent },
    input: {
      borderWidth: 1,
      borderColor: palette.border,
      borderRadius: radii.sm,
      paddingHorizontal: 10,
      paddingVertical: 8,
      color: palette.text,
      fontSize: 13,
    },
    hint: { color: palette.textDim, fontSize: 12 },
    error: { color: palette.danger, fontSize: 12 },
    editorActions: { flexDirection: "row", gap: spacing.sm },
    editorAction: { flex: 1 },
  })
}
