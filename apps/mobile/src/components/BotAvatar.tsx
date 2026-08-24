import { useMemo } from "react"
import { StyleSheet, Text, View } from "react-native"
import { botColors, brandNavy, logoInk } from "@nesqbot/ui"
import { useTheme } from "../theme"

export interface BotAvatarProps {
  name: string
  slug?: string
  size?: number
}

export function botInitials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return "??"
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

export function botColor(slug?: string): string {
  if (!slug) return logoInk
  return botColors[slug] ?? logoInk
}

export function BotAvatar({ name, slug, size = 40 }: BotAvatarProps): JSX.Element {
  const { radii } = useTheme()
  const background = botColor(slug)
  const style = useMemo(
    () => ({
      width: size,
      height: size,
      borderRadius: size >= 36 ? radii.md : radii.sm,
      backgroundColor: background,
    }),
    [size, radii, background],
  )

  return (
    <View style={[styles.avatar, style]} accessible accessibilityLabel={`${name} avatar`}>
      <Text style={[styles.text, { fontSize: Math.max(10, size * 0.32) }]}>{botInitials(name)}</Text>
    </View>
  )
}

const styles = StyleSheet.create({
  avatar: { alignItems: "center", justifyContent: "center" },
  text: { color: brandNavy, fontWeight: "800" },
})
