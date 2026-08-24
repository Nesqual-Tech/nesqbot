import { ActivityIndicator, StyleSheet, Text, View } from "react-native"
import { useTheme } from "../theme"

export interface SpinnerProps {
  label?: string
  size?: "small" | "large"
  /** Fills the available space and centres itself. */
  fill?: boolean
}

export function Spinner({ label, size = "large", fill = true }: SpinnerProps): JSX.Element {
  const { palette } = useTheme()
  return (
    <View
      style={[styles.root, fill && styles.fill]}
      accessibilityRole="progressbar"
      accessibilityLabel={label ?? "Loading"}
    >
      <ActivityIndicator size={size} color={palette.accent} />
      {label ? <Text style={[styles.label, { color: palette.textMuted }]}>{label}</Text> : null}
    </View>
  )
}

const styles = StyleSheet.create({
  root: { alignItems: "center", justifyContent: "center", gap: 10, padding: 24 },
  fill: { flex: 1 },
  label: { fontSize: 13 },
})
