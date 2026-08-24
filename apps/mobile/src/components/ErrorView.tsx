import { useMemo } from "react"
import { StyleSheet, Text, View } from "react-native"
import { ApiError, errorMessage, getApiBaseUrl, isOffline } from "../api/client"
import { useTheme, type Theme } from "../theme"
import { Button } from "./Button"

export interface ErrorViewProps {
  error: unknown
  onRetry?: () => void
  /** Prefixed above the error detail, e.g. "Could not load approvals". */
  title?: string
  compact?: boolean
}

/**
 * The single failure surface for every screen: shows the API's `{code, detail}` when we
 * have one, an offline hint (with the base URL in use) when the request never landed,
 * and always offers a retry.
 */
export function ErrorView({ error, onRetry, title, compact = false }: ErrorViewProps): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])

  const offline = isOffline(error)
  const heading = title ?? (offline ? "Cannot reach the API" : "Something went wrong")
  const code = error instanceof ApiError ? error.code : undefined

  return (
    <View
      style={[styles.root, compact && styles.compact]}
      accessible
      accessibilityRole="alert"
      accessibilityLabel={`${heading}. ${errorMessage(error)}`}
    >
      <Text style={styles.glyph}>{offline ? "⚠" : "✕"}</Text>
      <Text style={styles.title}>{heading}</Text>
      <Text style={styles.detail}>{errorMessage(error)}</Text>
      {offline ? <Text style={styles.hint}>Trying {getApiBaseUrl()}</Text> : null}
      {code ? <Text style={styles.hint}>{code}</Text> : null}
      {onRetry ? <Button label="Try again" onPress={onRetry} variant="secondary" /> : null}
    </View>
  )
}

function makeStyles(theme: Theme) {
  const { palette, spacing, radii } = theme
  return StyleSheet.create({
    root: {
      alignItems: "center",
      justifyContent: "center",
      gap: spacing.sm,
      padding: spacing.xl,
    },
    compact: {
      padding: spacing.lg,
      backgroundColor: palette.surface,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: palette.border,
    },
    glyph: { fontSize: 26, color: palette.danger },
    title: { color: palette.text, fontWeight: "700", fontSize: 16, textAlign: "center" },
    detail: {
      color: palette.textMuted,
      fontSize: 14,
      textAlign: "center",
      lineHeight: 20,
      maxWidth: 340,
    },
    hint: { color: palette.textDim, fontSize: 12, textAlign: "center" },
  })
}
