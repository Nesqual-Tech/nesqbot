import { useMemo } from "react"
import { StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native"
import { useTheme, type Palette, type Theme } from "../theme"

export type BadgeTone = "neutral" | "accent" | "success" | "warning" | "danger"

export interface BadgeProps {
  label: string
  tone?: BadgeTone
  /** Screen-reader text when the label alone is cryptic (e.g. a risk class). */
  accessibilityLabel?: string
  style?: StyleProp<ViewStyle>
}

export function toneColor(palette: Palette, tone: BadgeTone): string {
  switch (tone) {
    case "accent":
      return palette.accent
    case "success":
      return palette.success
    case "warning":
      return palette.warning
    case "danger":
      return palette.danger
    default:
      return palette.textMuted
  }
}

export function Badge({ label, tone = "neutral", accessibilityLabel, style }: BadgeProps): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const color = toneColor(theme.palette, tone)

  return (
    <View
      style={[styles.badge, { borderColor: color }, style]}
      accessible
      accessibilityLabel={accessibilityLabel ?? label}
    >
      <Text style={[styles.text, { color }]} numberOfLines={1}>
        {label}
      </Text>
    </View>
  )
}

function makeStyles(theme: Theme) {
  return StyleSheet.create({
    badge: {
      alignSelf: "flex-start",
      borderWidth: 1,
      borderRadius: theme.radii.sm,
      paddingHorizontal: 8,
      paddingVertical: 3,
      backgroundColor: theme.palette.surfaceAlt,
    },
    text: { fontSize: 11, fontWeight: "700", letterSpacing: 0.6 },
  })
}
