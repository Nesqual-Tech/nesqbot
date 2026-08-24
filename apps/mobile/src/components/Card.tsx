import { useMemo, type ReactNode } from "react"
import { Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native"
import { useTheme, type Theme } from "../theme"

export interface CardProps {
  children: ReactNode
  title?: string
  subtitle?: string
  right?: ReactNode
  onPress?: () => void
  accessibilityLabel?: string
  accessibilityHint?: string
  style?: StyleProp<ViewStyle>
  testID?: string
}

export function Card({
  children,
  title,
  subtitle,
  right,
  onPress,
  accessibilityLabel,
  accessibilityHint,
  style,
  testID,
}: CardProps): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])

  const header =
    title || subtitle || right ? (
      <View style={styles.header}>
        <View style={styles.headerText}>
          {title ? <Text style={styles.title}>{title}</Text> : null}
          {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
        </View>
        {right}
      </View>
    ) : null

  const body = (
    <>
      {header}
      {children}
    </>
  )

  if (!onPress) {
    return (
      <View style={[styles.card, style]} testID={testID}>
        {body}
      </View>
    )
  }

  return (
    <Pressable
      testID={testID}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? title}
      accessibilityHint={accessibilityHint}
      style={({ pressed }) => [styles.card, pressed && styles.pressed, style]}
    >
      {body}
    </Pressable>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii, spacing } = theme
  return StyleSheet.create({
    card: {
      backgroundColor: palette.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: palette.border,
      padding: spacing.lg,
      gap: spacing.sm,
    },
    pressed: { backgroundColor: palette.surfacePressed },
    header: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
    headerText: { flex: 1, gap: 2 },
    title: { color: palette.text, fontWeight: "700", fontSize: 15 },
    subtitle: { color: palette.textMuted, fontSize: 13 },
  })
}
