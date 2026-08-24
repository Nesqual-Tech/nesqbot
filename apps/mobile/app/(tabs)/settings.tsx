import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Alert,
  KeyboardAvoidingView,
  Linking,
  Platform,
  Pressable,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from "react-native"
import Constants from "expo-constants"
import { productName } from "@nesqbot/ui"
import {
  errorMessage,
  getApiBaseUrl,
  getDefaultApiBaseUrl,
  getEasProjectId,
  normalizeBaseUrl,
} from "../../src/api/client"
import { health } from "../../src/api/endpoints"
import { getEntraRedirectUri, useAuth } from "../../src/auth"
import { Badge, Button, Card, Screen } from "../../src/components"
import { updatePreferences, usePreferences, type ThemeMode } from "../../src/storage/preferences"
import {
  describeRegistration,
  getPermissionStatus,
  registerForPushNotifications,
  type PushRegistrationResult,
} from "../../src/notifications"
import { useTheme, type Theme } from "../../src/theme"

const THEME_OPTIONS: { label: string; value: ThemeMode }[] = [
  { label: "System", value: "system" },
  { label: "Light", value: "light" },
  { label: "Dark", value: "dark" },
]

type Probe =
  { state: "idle" } | { state: "checking" } | { state: "ok"; version?: string } | { state: "failed"; message: string }

export default function SettingsScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const preferences = usePreferences()
  const { user, signOut } = useAuth()

  const [serverDraft, setServerDraft] = useState(getApiBaseUrl())
  const [probe, setProbe] = useState<Probe>({ state: "idle" })
  const [push, setPush] = useState<PushRegistrationResult | null>(null)
  const [pushBusy, setPushBusy] = useState(false)
  const [permission, setPermission] = useState<string | null>(null)

  /**
   * Read the OS permission on mount.
   *
   * Without this the card can only describe a registration attempt the person
   * made in this session, so someone who denied notifications last week sees
   * "this device registers on start" — which is false, and is exactly the
   * misinformation that makes a missed approval look like a server problem.
   */
  useEffect(() => {
    let cancelled = false
    void getPermissionStatus().then((snapshot) => {
      if (!cancelled) setPermission(snapshot.granted ? "granted" : snapshot.status)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const saveServer = useCallback(async () => {
    const trimmed = serverDraft.trim()
    await updatePreferences({
      apiBaseUrl: trimmed.length > 0 ? normalizeBaseUrl(trimmed) : null,
    })
    setProbe({ state: "checking" })
    try {
      const result = await health.shallow({ timeoutMs: 8000 })
      setProbe({ state: "ok", version: result.version })
    } catch (caught) {
      setProbe({ state: "failed", message: errorMessage(caught) })
    }
  }, [serverDraft])

  const resetServer = useCallback(async () => {
    await updatePreferences({ apiBaseUrl: null })
    setServerDraft(getDefaultApiBaseUrl())
    setProbe({ state: "idle" })
  }, [])

  const toggleNotifications = useCallback(async (enabled: boolean) => {
    await updatePreferences({ notificationsEnabled: enabled })
    if (!enabled) {
      setPush(null)
      return
    }
    setPushBusy(true)
    try {
      setPush(await registerForPushNotifications(getEasProjectId()))
    } finally {
      setPushBusy(false)
    }
  }, [])

  const confirmSignOut = useCallback(() => {
    Alert.alert("Sign out?", "You will need to sign in again to approve anything.", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ])
  }, [signOut])

  const appVersion = Constants.expoConfig?.version ?? "0.0.0"

  return (
    <KeyboardAvoidingView style={styles.flex} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Screen testID="settings-screen">
        <Card title="Account" subtitle={user?.email ?? "Not signed in"}>
          <Text style={styles.value}>{user?.display_name ?? "Unknown user"}</Text>
          <Button
            label="Sign out"
            variant="danger"
            onPress={confirmSignOut}
            accessibilityLabel="Sign out of Nesq Bot"
          />
        </Card>

        <Card
          title="API server"
          subtitle={preferences.apiBaseUrl ? "Custom override" : "Using the app default"}
          right={
            probe.state === "ok" ? (
              <Badge label="REACHABLE" tone="success" />
            ) : probe.state === "failed" ? (
              <Badge label="UNREACHABLE" tone="danger" />
            ) : null
          }
        >
          <TextInput
            style={styles.input}
            value={serverDraft}
            onChangeText={setServerDraft}
            placeholder={getDefaultApiBaseUrl()}
            placeholderTextColor={theme.palette.textDim}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            accessibilityLabel="API base URL"
          />
          <Text style={styles.hint}>
            Include the /api suffix. Changing this signs requests against a different backend immediately.
          </Text>
          {probe.state === "failed" ? <Text style={styles.error}>{probe.message}</Text> : null}
          {probe.state === "ok" && probe.version ? <Text style={styles.hint}>API version {probe.version}</Text> : null}
          <View style={styles.row}>
            <Button
              label="Save and test"
              size="sm"
              onPress={() => void saveServer()}
              loading={probe.state === "checking"}
              style={styles.rowItem}
              accessibilityLabel="Save the API URL and test the connection"
            />
            <Button
              label="Reset"
              size="sm"
              variant="ghost"
              onPress={() => void resetServer()}
              style={styles.rowItem}
              accessibilityLabel="Reset to the default API URL"
            />
          </View>
        </Card>

        <Card title="Theme" subtitle="Follows your device unless you pick one.">
          <View style={styles.row}>
            {THEME_OPTIONS.map((option) => {
              const active = preferences.themeMode === option.value
              return (
                <Pressable
                  key={option.value}
                  onPress={() => void updatePreferences({ themeMode: option.value })}
                  accessibilityRole="radio"
                  accessibilityState={{ selected: active, checked: active }}
                  accessibilityLabel={`${option.label} theme`}
                  style={[styles.chip, active && styles.chipActive]}
                >
                  <Text style={[styles.chipText, active && styles.chipTextActive]}>{option.label}</Text>
                </Pressable>
              )
            })}
          </View>
        </Card>

        <Card
          title="Approval alerts"
          subtitle="Push a notification when a bot needs a decision."
          right={
            <Switch
              value={preferences.notificationsEnabled}
              onValueChange={(next) => void toggleNotifications(next)}
              disabled={pushBusy}
              accessibilityLabel="Approval push notifications"
              trackColor={{ true: theme.palette.accent, false: theme.palette.border }}
              thumbColor={theme.palette.surface}
            />
          }
        >
          {push ? (
            <>
              <Text style={styles.hint}>{describeRegistration(push)}</Text>
              {push.status === "denied" ? (
                <Button
                  label="Open system settings"
                  size="sm"
                  variant="ghost"
                  onPress={() => void Linking.openSettings()}
                  accessibilityLabel="Open the system notification settings"
                />
              ) : null}
              {push.status !== "registered" ? (
                <Button
                  label="Try again"
                  size="sm"
                  variant="ghost"
                  onPress={() => void toggleNotifications(true)}
                  loading={pushBusy}
                  accessibilityLabel="Try registering this device for alerts again"
                />
              ) : null}
            </>
          ) : (
            <Text style={styles.hint}>
              {!preferences.notificationsEnabled
                ? "Alerts are off. You will only see approvals when you open the app."
                : permission === "granted"
                  ? "This device registers for alerts when the app starts."
                  : permission === null
                    ? "Checking…"
                    : `Notifications are ${permission} at the system level, so nothing will arrive.`}
            </Text>
          )}

          {/* Said plainly because the gap is real and silence about it is the
              thing that costs someone a stalled task. The API pushes approvals
              (services/notifications.py) and nothing else; a run parked on a
              human is found by opening the app. */}
          <Text style={styles.hint}>
            Only approvals are pushed. When a bot needs you at its screen, it waits in the Inbox — nothing buzzes.
          </Text>
        </Card>

        <Card title="About">
          <Row label={productName} value={`v${appVersion}`} styles={styles} />
          <Row label="API" value={getApiBaseUrl()} styles={styles} />
          <Row label="Entra redirect" value={getEntraRedirectUri()} styles={styles} />
          <Row label="EAS project" value={getEasProjectId() ?? "not configured"} styles={styles} />
          {/* An Expo push token needs both an EAS project id and a build with
              real APNs credentials. The id is the half that is now done, and
              distinguishing the two stops "notifications do not work" from
              being investigated at the wrong end. */}
          <Row
            label="Push tokens"
            value={getEasProjectId() ? "project linked" : "blocked — no EAS project"}
            styles={styles}
          />
        </Card>
      </Screen>
    </KeyboardAvoidingView>
  )
}

function Row({
  label,
  value,
  styles,
}: {
  label: string
  value: string
  styles: ReturnType<typeof makeStyles>
}): JSX.Element {
  return (
    <View style={styles.aboutRow}>
      <Text style={styles.aboutLabel}>{label}</Text>
      <Text style={styles.aboutValue} numberOfLines={2} selectable>
        {value}
      </Text>
    </View>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    flex: { flex: 1 },
    value: { color: palette.text, fontSize: 15, fontWeight: "600" },
    hint: { color: palette.textDim, fontSize: 12, lineHeight: 17 },
    error: { color: palette.danger, fontSize: 12 },
    row: { flexDirection: "row", gap: spacing.sm },
    rowItem: { flex: 1 },
    chip: {
      flex: 1,
      minHeight: 44,
      alignItems: "center",
      justifyContent: "center",
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: palette.border,
      backgroundColor: palette.surfaceAlt,
    },
    chipActive: { backgroundColor: palette.accent, borderColor: palette.accent },
    chipText: { color: palette.textMuted, fontWeight: "600", fontSize: 13 },
    chipTextActive: { color: theme.onAccent },
    input: {
      minHeight: 44,
      borderRadius: radii.md,
      paddingHorizontal: spacing.md,
      color: palette.text,
      backgroundColor: palette.surfaceAlt,
      borderWidth: 1,
      borderColor: palette.border,
    },
    aboutRow: { flexDirection: "row", justifyContent: "space-between", gap: spacing.md },
    aboutLabel: { color: palette.textMuted, fontSize: 13 },
    aboutValue: { color: palette.text, fontSize: 13, flexShrink: 1, textAlign: "right" },
  })
}
