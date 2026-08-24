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
import { useLocalSearchParams, useRouter } from "expo-router"
import type { Approval, Bot } from "@nesqbot/protocol"
import { approvals as approvalsApi, bots as botsApi } from "../../src/api/endpoints"
import { ApiError } from "../../src/api/client"
import { approvalExecutionOutcome, parseApprovalPayload, type ApprovalPayload } from "@nesqbot/protocol"
import type { ApprovalContinuation, ApprovalDecisionResult } from "../../src/api/types"
import { useAuth } from "../../src/auth"
import {
  Badge,
  Button,
  Card,
  ErrorView,
  RiskBadge,
  Screen,
  requiresConfirmation,
  riskDescription,
} from "../../src/components"
import { useAsync } from "../../src/hooks/useAsync"
import { refreshInbox, removePendingApproval } from "../../src/state/inbox"
import { useTheme, type Theme } from "../../src/theme"
import { formatDate, humanize, prettyJson, relativeAge } from "../../src/utils/format"

interface DetailData {
  approval: Approval
  botName: string
}

type Decision = "approved" | "rejected"

export default function ApprovalDetailScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const { id } = useLocalSearchParams<{ id: string }>()
  const router = useRouter()
  const { status } = useAuth()
  const authenticated = status === "authenticated"

  const [note, setNote] = useState("")
  const [confirming, setConfirming] = useState<Decision | null>(null)
  const [submitting, setSubmitting] = useState<Decision | null>(null)
  const [decisionError, setDecisionError] = useState<unknown>(null)
  const [outcome, setOutcome] = useState<ApprovalDecisionResult | null>(null)

  const load = useCallback(
    async (signal: AbortSignal): Promise<DetailData> => {
      const approval = await approvalsApi.get(String(id), { signal })
      let botName = "Unknown bot"
      try {
        const bot: Bot = await botsApi.get(approval.bot_id, { signal })
        botName = bot.name
      } catch {
        /* the approval matters more than the bot's display name */
      }
      return { approval, botName }
    },
    [id],
  )

  const state = useAsync<DetailData>(load, [id], { enabled: authenticated && Boolean(id) })

  const approval = outcome ?? state.data?.approval ?? null
  const rawPayload = (approval?.payload ?? {}) as Record<string, unknown>
  const parsedPayload = parseApprovalPayload(approval?.payload)
  const decided = approval !== null && approval.status !== "pending"

  const submit = useCallback(
    async (decision: Decision) => {
      if (!approval) return
      setConfirming(null)
      setSubmitting(decision)
      setDecisionError(null)
      try {
        const result = await approvalsApi.decide(approval.id, decision, note)
        setOutcome(result)
        removePendingApproval(approval.id)
        // Deciding can also *unpark a run*, which may in turn park it again on
        // a takeover or another approval. Re-read the inbox so the badge
        // reflects what the continuation actually did, not what was true before.
        void refreshInbox().catch(() => undefined)
      } catch (caught) {
        setDecisionError(caught)
        // 409 means somebody (or the sweeper) already decided it -- resync.
        if (caught instanceof ApiError && caught.status === 409) void state.refresh()
      } finally {
        setSubmitting(null)
      }
    },
    [approval, note, state],
  )

  const onDecide = useCallback(
    (decision: Decision) => {
      if (!approval) return
      // High-risk actions are irreversible: always take a second confirmation.
      if (decision === "approved" && requiresConfirmation(approval.risk)) {
        setConfirming(decision)
        return
      }
      void submit(decision)
    },
    [approval, submit],
  )

  const execution = outcome?.execution ?? null
  /** Which of the three execution arms this is. Never re-derived inline. */
  const executed = approvalExecutionOutcome(execution)
  const continuation: ApprovalContinuation | null = execution?.continuation ?? null

  return (
    <>
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        keyboardVerticalOffset={Platform.OS === "ios" ? 90 : 0}
      >
        <Screen
          loading={state.loading}
          error={state.data === null && !outcome ? (state.error ?? undefined) : undefined}
          errorTitle="Could not load this approval"
          onRetry={() => void state.reload()}
          refreshing={state.refreshing}
          onRefresh={() => void state.refresh()}
          testID="approval-detail"
        >
          {approval ? (
            <>
              <Card>
                <View style={styles.headerRow}>
                  <RiskBadge risk={approval.risk} />
                  <Badge
                    label={approval.status.toUpperCase()}
                    tone={
                      approval.status === "approved" ? "success" : approval.status === "pending" ? "warning" : "neutral"
                    }
                  />
                </View>
                <Text style={styles.title}>{approval.title}</Text>
                {approval.summary ? <Text style={styles.summary}>{approval.summary}</Text> : null}
                <Text style={styles.meta}>
                  {state.data?.botName ?? "Bot"} · {relativeAge(approval.created_at)} ·{" "}
                  {formatDate(approval.created_at)}
                </Text>
                <Text style={styles.riskHint}>{riskDescription(approval.risk)}</Text>
              </Card>

              <PayloadCard parsed={parsedPayload} raw={rawPayload} styles={styles} />

              {/* Three arms, not two.
                  `ok: true`  — approved and it ran.
                  `ok: false` — approved, then refused at execution. An approved
                                DOM click is re-resolved against the page as it is
                                now and can honestly refuse; "approved" and "ran"
                                are different facts.
                  no `ok`     — rejected, but the task carried on, so the envelope
                                carries only `continuation`. Reading a missing
                                `ok` as false renders an ordinary rejection as an
                                execution failure, which is what this used to do. */}
              {executed === "ran" || executed === "failed" ? (
                <Card
                  title="Execution result"
                  subtitle={executed === "ran" ? "The held action ran." : "It was approved, but it did not run."}
                >
                  <Badge
                    label={executed === "ran" ? "RAN" : "REFUSED AT EXECUTION"}
                    tone={executed === "ran" ? "success" : "danger"}
                  />
                  {execution?.ok === true ? (
                    execution.result !== undefined && execution.result !== null ? (
                      <ScrollView horizontal style={styles.codeScroll}>
                        <Text style={styles.code} selectable>
                          {prettyJson(execution.result)}
                        </Text>
                      </ScrollView>
                    ) : null
                  ) : null}
                  {execution?.ok === false ? <Text style={styles.error}>{execution.error}</Text> : null}
                </Card>
              ) : null}

              {outcome && executed === "not-executed" ? (
                <Card title="Decision recorded">
                  <Text style={styles.summary}>
                    {outcome.status === "approved"
                      ? "Approved. The API did not return an execution result for this payload."
                      : "Rejected. Nothing was executed, and the bot has been told this was a person's decision — not an error to route around."}
                  </Text>
                </Card>
              ) : null}

              {/* The half of the feature that makes an approval worth deciding
                  from a phone: the task does not stop at the decision, it picks
                  itself back up. Without saying so, a person who approves a
                  step in a thirty-step task has no idea whether the other
                  twenty-nine are still going to happen. */}
              {continuation ? (
                <Card
                  title={continuation.continued ? "The task carried on" : "The task had already moved on"}
                  subtitle={
                    continuation.continued
                      ? "Your decision handed the bot its answer and it picked the same task back up."
                      : "Something else continued this run first, so nothing was started twice."
                  }
                >
                  <Badge
                    label={continuation.continued ? "RESUMED" : "NO CHANGE"}
                    tone={continuation.continued ? "success" : "neutral"}
                  />
                  {continuation.outcome ? <Text style={styles.meta}>Ended: {continuation.outcome}</Text> : null}
                  {continuation.status ? <Text style={styles.meta}>Run is now {continuation.status}.</Text> : null}
                  {continuation.error ? (
                    <Text style={styles.error}>
                      The decision stands, but the bot could not carry on: {continuation.error}
                    </Text>
                  ) : null}
                  {/* A resumed run can park again immediately — on a person at
                      the screen this time. That is the next thing to do, so it
                      is a button rather than a sentence. */}
                  {continuation.status === "awaiting_human" ? (
                    <Button
                      label="It needs you at the screen now"
                      size="sm"
                      onPress={() =>
                        router.push({ pathname: "/takeover/[runId]", params: { runId: continuation.run_id } })
                      }
                      accessibilityLabel="Open the handover the resumed task is now waiting on"
                    />
                  ) : null}
                </Card>
              ) : null}

              {decided ? null : (
                <Card title="Note" subtitle="Optional. Stored with your decision.">
                  <TextInput
                    style={styles.input}
                    value={note}
                    onChangeText={setNote}
                    placeholder="Why are you approving or rejecting?"
                    placeholderTextColor={theme.palette.textDim}
                    multiline
                    accessibilityLabel="Decision note"
                    maxLength={1000}
                  />
                </Card>
              )}

              {decisionError ? (
                <ErrorView
                  error={decisionError}
                  compact
                  title="The decision did not go through"
                  onRetry={() => setDecisionError(null)}
                />
              ) : null}

              {decided ? (
                <Text style={styles.meta}>This approval is {approval.status} and can no longer be changed.</Text>
              ) : (
                <View style={styles.actions}>
                  <Button
                    label="Approve"
                    onPress={() => onDecide("approved")}
                    loading={submitting === "approved"}
                    disabled={submitting !== null}
                    accessibilityLabel={`Approve: ${approval.title}`}
                    accessibilityHint={riskDescription(approval.risk)}
                    style={styles.action}
                  />
                  <Button
                    label="Reject"
                    variant="ghost"
                    onPress={() => onDecide("rejected")}
                    loading={submitting === "rejected"}
                    disabled={submitting !== null}
                    accessibilityLabel={`Reject: ${approval.title}`}
                    style={styles.action}
                  />
                </View>
              )}
            </>
          ) : null}
        </Screen>
      </KeyboardAvoidingView>

      <ConfirmSheet
        visible={confirming !== null}
        title={approval ? `Approve "${approval.title}"?` : "Approve?"}
        risk={approval?.risk ?? "send"}
        detail={approval ? riskDescription(approval.risk) : ""}
        onCancel={() => setConfirming(null)}
        onConfirm={() => void submit("approved")}
        styles={styles}
      />
    </>
  )
}

/**
 * Renders the held payload readably.
 *
 * `parseApprovalPayload` returns null for rows that predate the `kind` discriminator,
 * so the raw JSON dump stays as the honest fallback rather than a blank card.
 */
function PayloadCard({
  parsed,
  raw,
  styles,
}: {
  parsed: ApprovalPayload | null
  raw: Record<string, unknown>
  styles: ReturnType<typeof makeStyles>
}): JSX.Element {
  if (parsed?.kind === "connector_action") {
    const input = parsed.input ?? {}
    return (
      <Card title="Held action" subtitle={`${humanize(parsed.connector_id)} · ${humanize(parsed.action)}`}>
        {parsed.draft ? <DraftBlock draft={parsed.draft} styles={styles} /> : null}
        {Object.keys(input).length > 0 ? (
          <FieldList fields={Object.entries(input)} styles={styles} />
        ) : (
          <Text style={styles.meta}>No inputs recorded.</Text>
        )}
      </Card>
    )
  }

  if (parsed?.kind === "message_only") {
    return (
      <Card title="Message" subtitle={parsed.to ? `To ${parsed.to}` : undefined}>
        <DraftBlock draft={parsed.draft} styles={styles} />
      </Card>
    )
  }

  if (parsed?.kind === "mcp_tool") {
    const args = parsed.arguments ?? {}
    return (
      <Card title="MCP tool call" subtitle={humanize(parsed.tool)}>
        {parsed.draft ? <DraftBlock draft={parsed.draft} styles={styles} /> : null}
        {Object.keys(args).length > 0 ? (
          <FieldList fields={Object.entries(args)} styles={styles} />
        ) : (
          <Text style={styles.meta}>No arguments.</Text>
        )}
      </Card>
    )
  }

  if (parsed?.kind === "desktop_steps") {
    const steps = parsed.steps ?? []
    return (
      <Card title="Desktop steps" subtitle={`${steps.length} step${steps.length === 1 ? "" : "s"} to replay`}>
        {parsed.draft ? <DraftBlock draft={parsed.draft} styles={styles} /> : null}
        {steps.map((step, index) => (
          <View key={`${step.action}-${index}`} style={styles.field}>
            <Text style={styles.fieldKey}>{index + 1}</Text>
            <Text style={styles.fieldValue}>
              {humanize(step.action)}
              {step.text ? ` "${step.text}"` : ""}
              {typeof step.x === "number" && typeof step.y === "number" ? ` at ${step.x}, ${step.y}` : ""}
              {step.keys && step.keys.length > 0 ? ` [${step.keys.join(" + ")}]` : ""}
            </Text>
          </View>
        ))}
      </Card>
    )
  }

  return (
    <Card title="Held payload" subtitle="This payload predates the typed format.">
      <ScrollView horizontal style={styles.codeScroll}>
        <Text style={styles.code} selectable>
          {prettyJson(raw)}
        </Text>
      </ScrollView>
    </Card>
  )
}

function DraftBlock({ draft, styles }: { draft: string; styles: ReturnType<typeof makeStyles> }): JSX.Element {
  return (
    <View style={styles.draftBox}>
      <Text style={styles.draftLabel}>DRAFT</Text>
      <Text style={styles.draft} selectable>
        {draft}
      </Text>
    </View>
  )
}

function FieldList({
  fields,
  styles,
}: {
  fields: [string, unknown][]
  styles: ReturnType<typeof makeStyles>
}): JSX.Element {
  return (
    <View style={styles.fields}>
      {fields.map(([key, value]) => (
        <View key={key} style={styles.field}>
          <Text style={styles.fieldKey}>{humanize(key)}</Text>
          <Text style={styles.fieldValue} selectable>
            {typeof value === "string" ? value : prettyJson(value)}
          </Text>
        </View>
      ))}
    </View>
  )
}

/* -------------------------------------------------------------- confirmation */

function ConfirmSheet({
  visible,
  title,
  risk,
  detail,
  onCancel,
  onConfirm,
  styles,
}: {
  visible: boolean
  title: string
  risk: string
  detail: string
  onCancel: () => void
  onConfirm: () => void
  styles: ReturnType<typeof makeStyles>
}): JSX.Element {
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onCancel} accessibilityViewIsModal>
      <View style={styles.sheetBackdrop}>
        <Pressable
          style={styles.sheetScrim}
          onPress={onCancel}
          accessibilityRole="button"
          accessibilityLabel="Dismiss without approving"
        />
        <View style={styles.sheet}>
          <View style={styles.sheetHandle} />
          <Text style={styles.sheetTitle}>{title}</Text>
          <View style={styles.headerRow}>
            <RiskBadge risk={risk} />
          </View>
          <Text style={styles.summary}>{detail}</Text>
          <Text style={styles.meta}>
            Approving runs the held action immediately. This cannot be undone from the app.
          </Text>
          <View style={styles.actions}>
            <Button
              label="Yes, approve"
              variant="danger"
              onPress={onConfirm}
              accessibilityLabel="Confirm approval"
              style={styles.action}
            />
            <Button
              label="Cancel"
              variant="ghost"
              onPress={onCancel}
              accessibilityLabel="Cancel approval"
              style={styles.action}
            />
          </View>
        </View>
      </View>
    </Modal>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    flex: { flex: 1 },
    headerRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    title: { color: palette.text, fontWeight: "800", fontSize: 18 },
    summary: { color: palette.textMuted, fontSize: 14, lineHeight: 20 },
    meta: { color: palette.textDim, fontSize: 12 },
    riskHint: { color: palette.warning, fontSize: 12 },
    error: { color: palette.danger, fontSize: 13 },
    fields: { gap: spacing.md },
    field: { gap: 2 },
    fieldKey: { color: palette.textDim, fontSize: 11, fontWeight: "700", letterSpacing: 0.6 },
    fieldValue: { color: palette.text, fontSize: 14, lineHeight: 20 },
    draftBox: {
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
      padding: spacing.md,
      gap: spacing.xs,
    },
    draftLabel: { color: palette.textDim, fontSize: 10, fontWeight: "700", letterSpacing: 1 },
    draft: { color: palette.text, fontSize: 14, lineHeight: 21 },
    codeScroll: {
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.md,
      padding: spacing.md,
    },
    code: {
      color: palette.textMuted,
      fontSize: 12,
      fontFamily: Platform.OS === "ios" ? "Menlo" : "monospace",
    },
    input: {
      minHeight: 80,
      maxHeight: 160,
      color: palette.text,
      backgroundColor: palette.surfaceAlt,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
      padding: spacing.md,
      textAlignVertical: "top",
    },
    actions: { flexDirection: "row", gap: spacing.sm, marginTop: spacing.xs },
    action: { flex: 1 },
    sheetBackdrop: { flex: 1, justifyContent: "flex-end" },
    // Scrim built from the theme background rather than a literal colour.
    sheetScrim: { ...StyleSheet.absoluteFillObject, backgroundColor: palette.bg, opacity: 0.75 },
    sheet: {
      backgroundColor: palette.surface,
      borderTopLeftRadius: radii.xl,
      borderTopRightRadius: radii.xl,
      borderTopWidth: 1,
      borderColor: palette.border,
      padding: spacing.xl,
      paddingBottom: spacing.xxl,
      gap: spacing.md,
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
  })
}
