import { useMemo } from "react"
import { StyleSheet, Text, View } from "react-native"
import { useTheme, type Theme } from "../theme"
import { Button } from "./Button"

export interface EmptyStateProps {
  title: string
  message?: string
  /** A short glyph shown above the title. */
  glyph?: string
  actionLabel?: string
  onAction?: () => void
}

export function EmptyState({ title, message, glyph = "○", actionLabel, onAction }: EmptyStateProps): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])

  return (
    <View style={styles.root} accessible accessibilityLabel={message ? `${title}. ${message}` : title}>
      <Text style={styles.glyph}>{glyph}</Text>
      <Text style={styles.title}>{title}</Text>
      {message ? <Text style={styles.message}>{message}</Text> : null}
      {actionLabel && onAction ? <Button label={actionLabel} onPress={onAction} variant="secondary" /> : null}
    </View>
  )
}

function makeStyles(theme: Theme) {
  const { palette, spacing } = theme
  return StyleSheet.create({
    root: {
      alignItems: "center",
      justifyContent: "center",
      gap: spacing.sm,
      paddingVertical: spacing.xxl,
      paddingHorizontal: spacing.lg,
    },
    glyph: { fontSize: 30, color: palette.textDim },
    title: { color: palette.text, fontWeight: "700", fontSize: 16, textAlign: "center" },
    message: {
      color: palette.textMuted,
      fontSize: 14,
      textAlign: "center",
      lineHeight: 20,
      maxWidth: 320,
    },
  })
}
