import { useCallback, useMemo, useState } from "react"
import { KeyboardAvoidingView, Platform, StyleSheet, Text, TextInput, View } from "react-native"
import { companyName, productName } from "@nesqbot/ui"
import { errorMessage, getApiBaseUrl, getDefaultApiBaseUrl, normalizeBaseUrl } from "../src/api/client"
import { health } from "../src/api/endpoints"
import { isEntraConfigured, useAuth } from "../src/auth"
import { Badge, Button, Card, ErrorView, Screen } from "../src/components"
import { updatePreferences, usePreferences } from "../src/storage/preferences"
import { useTheme, type Theme } from "../src/theme"

type Probe = { state: "idle" } | { state: "checking" } | { state: "ok" } | { state: "failed"; message: string }

export default function LoginScreen(): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const { signInDev, signInWithEntra, error, clearError } = useAuth()
  const preferences = usePreferences()

  const [busy, setBusy] = useState<"dev" | "entra" | null>(null)
  const [showServer, setShowServer] = useState(false)
  const [serverDraft, setServerDraft] = useState(getApiBaseUrl())
  const [probe, setProbe] = useState<Probe>({ state: "idle" })

  const entraReady = isEntraConfigured()

  const run = useCallback(
    async (which: "dev" | "entra") => {
      setBusy(which)
      clearError()
      try {
        if (which === "dev") await signInDev()
        else await signInWithEntra()
      } catch {
        /* the provider stores the error; the view renders it */
      } finally {
        setBusy(null)
      }
    },
    [clearError, signInDev, signInWithEntra],
  )

  const saveServer = useCallback(async () => {
    const trimmed = serverDraft.trim()
    const next = trimmed.length > 0 ? normalizeBaseUrl(trimmed) : null
    await updatePreferences({ apiBaseUrl: next })
    setProbe({ state: "checking" })
    try {
      await health.shallow({ timeoutMs: 8000 })
      setProbe({ state: "ok" })
    } catch (caught) {
      setProbe({ state: "failed", message: errorMessage(caught) })
    }
  }, [serverDraft])

  return (
    <KeyboardAvoidingView style={styles.flex} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Screen contentContainerStyle={styles.content} testID="login-screen">
        <View style={styles.hero}>
          <Text style={styles.eyebrow}>{companyName.toUpperCase()}</Text>
          <Text style={styles.title}>{productName}</Text>
          <Text style={styles.subtitle}>Approve what your bots are about to do, from anywhere.</Text>
        </View>

        {error ? <ErrorView error={error} compact title="Sign-in failed" onRetry={clearError} /> : null}

        <Card>
          <Button
            label="Sign in with Microsoft"
            onPress={() => void run("entra")}
            loading={busy === "entra"}
            disabled={busy !== null || !entraReady}
            block
            accessibilityLabel="Sign in with your Microsoft work account"
            accessibilityHint={entraReady ? undefined : "Unavailable until Entra credentials are configured"}
          />
          {!entraReady ? (
            <Text style={styles.hint}>
              Entra sign-in needs EXPO_PUBLIC_ENTRA_TENANT_ID, EXPO_PUBLIC_ENTRA_CLIENT_ID and
              EXPO_PUBLIC_ENTRA_SCOPE.
            </Text>
          ) : null}

          <View style={styles.divider}>
            <View style={styles.rule} />
            <Text style={styles.dividerText}>or</Text>
            <View style={styles.rule} />
          </View>

          <Button
            label="Continue with dev login"
            variant="secondary"
            onPress={() => void run("dev")}
            loading={busy === "dev"}
            disabled={busy !== null}
            block
            accessibilityLabel="Continue with the development login"
            accessibilityHint="Only works when the API is not running in production"
          />
          <Text style={styles.hint}>Dev login is rejected by the API when NESQ_ENV is production.</Text>
        </Card>

        <Card
          title="API server"
          subtitle={preferences.apiBaseUrl ? "Custom" : "Default"}
          right={
            probe.state === "ok" ? (
              <Badge label="REACHABLE" tone="success" />
            ) : probe.state === "failed" ? (
              <Badge label="UNREACHABLE" tone="danger" />
            ) : null
          }
        >
          <Text style={styles.server} numberOfLines={2}>
            {getApiBaseUrl()}
          </Text>
          {showServer ? (
            <>
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
                Include the /api suffix. A phone cannot reach localhost — use your machine's LAN address.
              </Text>
              {probe.state === "failed" ? <Text style={styles.error}>{probe.message}</Text> : null}
              <View style={styles.row}>
                <Button
                  label={probe.state === "checking" ? "Checking…" : "Save and test"}
                  size="sm"
                  onPress={() => void saveServer()}
                  loading={probe.state === "checking"}
                  style={styles.rowItem}
                  accessibilityLabel="Save the API URL and test it"
                />
                <Button
                  label="Reset"
                  size="sm"
                  variant="ghost"
                  onPress={() => {
                    setServerDraft(getDefaultApiBaseUrl())
                    setProbe({ state: "idle" })
                    void updatePreferences({ apiBaseUrl: null })
                  }}
                  style={styles.rowItem}
                  accessibilityLabel="Reset to the default API URL"
                />
              </View>
            </>
          ) : (
            <Button
              label="Change server"
              size="sm"
              variant="ghost"
              onPress={() => {
                setServerDraft(getApiBaseUrl())
                setShowServer(true)
              }}
              accessibilityLabel="Change the API server address"
            />
          )}
        </Card>
      </Screen>
    </KeyboardAvoidingView>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    flex: { flex: 1 },
    content: { justifyContent: "center", gap: spacing.lg, paddingVertical: spacing.xxl },
    hero: { gap: spacing.xs, marginBottom: spacing.sm },
    eyebrow: { color: palette.textDim, letterSpacing: 2, fontSize: 11, fontWeight: "700" },
    title: { color: palette.text, fontSize: 34, fontWeight: "800" },
    subtitle: { color: palette.textMuted, fontSize: 15, lineHeight: 21 },
    hint: { color: palette.textDim, fontSize: 12, lineHeight: 17 },
    error: { color: palette.danger, fontSize: 12 },
    server: { color: palette.textMuted, fontSize: 13 },
    divider: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    rule: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: palette.border },
    dividerText: { color: palette.textDim, fontSize: 12 },
    row: { flexDirection: "row", gap: spacing.sm },
    rowItem: { flex: 1 },
    input: {
      minHeight: 44,
      borderRadius: radii.md,
      paddingHorizontal: spacing.md,
      color: palette.text,
      backgroundColor: palette.surfaceAlt,
      borderWidth: 1,
      borderColor: palette.border,
    },
  })
}
