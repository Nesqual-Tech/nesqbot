import { useMemo } from "react"
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native"
import { useTheme, type Theme } from "../theme"

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger"

export interface ButtonProps {
  label: string
  onPress: () => void
  variant?: ButtonVariant
  disabled?: boolean
  loading?: boolean
  /** Stretches to the width of its parent. */
  block?: boolean
  size?: "sm" | "md"
  accessibilityLabel?: string
  accessibilityHint?: string
  style?: StyleProp<ViewStyle>
  testID?: string
}

/** Minimum touch target (Apple HIG / Material both land on 44-48dp). */
const MIN_TOUCH = 44

export function Button({
  label,
  onPress,
  variant = "primary",
  disabled = false,
  loading = false,
  block = false,
  size = "md",
  accessibilityLabel,
  accessibilityHint,
  style,
  testID,
}: ButtonProps): JSX.Element {
  const theme = useTheme()
  const styles = useMemo(() => makeStyles(theme), [theme])
  const inactive = disabled || loading

  const variantStyle: Record<ButtonVariant, ViewStyle> = {
    primary: styles.primary,
    secondary: styles.secondary,
    ghost: styles.ghost,
    danger: styles.danger,
  }
  const textColor: Record<ButtonVariant, string> = {
    primary: theme.onAccent,
    secondary: theme.palette.text,
    ghost: theme.palette.textMuted,
    danger: theme.scheme === "light" ? theme.palette.surface : theme.palette.text,
  }

  return (
    <Pressable
      testID={testID}
      onPress={onPress}
      disabled={inactive}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityHint={accessibilityHint}
      accessibilityState={{ disabled: inactive, busy: loading }}
      hitSlop={6}
      style={({ pressed }) => [
        styles.base,
        size === "sm" && styles.sm,
        variantStyle[variant],
        block && styles.block,
        pressed && !inactive && styles.pressed,
        inactive && styles.disabled,
        style,
      ]}
    >
      <View style={styles.content}>
        {loading ? <ActivityIndicator size="small" color={textColor[variant]} /> : null}
        <Text style={[styles.label, size === "sm" && styles.labelSm, { color: textColor[variant] }]}>{label}</Text>
      </View>
    </Pressable>
  )
}

function makeStyles(theme: Theme) {
  const { palette, radii } = theme
  return StyleSheet.create({
    base: {
      minHeight: MIN_TOUCH,
      paddingHorizontal: 16,
      paddingVertical: 11,
      borderRadius: radii.md,
      alignItems: "center",
      justifyContent: "center",
      borderWidth: 1,
      borderColor: "transparent",
    },
    sm: { minHeight: MIN_TOUCH, paddingHorizontal: 12 },
    block: { alignSelf: "stretch" },
    content: { flexDirection: "row", alignItems: "center", gap: 8 },
    primary: { backgroundColor: palette.accent },
    secondary: { backgroundColor: palette.surfaceRaised, borderColor: palette.border },
    ghost: { backgroundColor: "transparent", borderColor: palette.border },
    danger: { backgroundColor: palette.danger },
    pressed: { opacity: 0.75 },
    disabled: { opacity: 0.45 },
    label: { fontWeight: "700", fontSize: 15 },
    labelSm: { fontSize: 13 },
  })
}
