/**
 * Theme wiring: `useColorScheme()` plus the user's stored override, resolved to one of
 * the two palettes exported by `@nesqbot/ui`. No screen should ever import a palette
 * directly -- they call `useTheme()` so light/dark both work.
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react"
import { AccessibilityInfo, useColorScheme, type TextStyle, type ViewStyle } from "react-native"
import {
  brandNavy,
  darkPalette,
  lightPalette,
  radii,
  radiusPill,
  spacing,
  type ElevationLevel,
  type MotionDurationStep,
  type TypeScaleStep,
} from "@nesqbot/ui"
import { motionRn, shadow as rnShadow, type as typeStyles, type RnMotion } from "./tokens"
import { usePreferences, type ThemeMode } from "../storage/preferences"

export type ColorScheme = "light" | "dark"

/**
 * Widened palette type. The tokens package declares its palettes `as const`, so
 * `typeof darkPalette` is a set of string literals that `lightPalette` is not
 * assignable to -- this mapped type keeps both interchangeable.
 */
export type Palette = { [K in keyof typeof darkPalette]: string }

export interface Theme {
  scheme: ColorScheme
  mode: ThemeMode
  palette: Palette
  /** Readable foreground for a filled accent surface. */
  onAccent: string
  radii: typeof radii
  radiusPill: typeof radiusPill
  spacing: typeof spacing
  /** `@nesqbot/ui` typeScale, converted to React Native `TextStyle`s. */
  type: Record<TypeScaleStep, TextStyle>
  /** Shadow props for an elevation level, already scaled for the active scheme. */
  shadow: (level: ElevationLevel) => ViewStyle
  /** Durations and easing curves, RN-shaped. */
  motion: RnMotion
  /** True when the OS has "reduce motion" enabled. */
  prefersReducedMotion: boolean
  /** Duration in ms for a motion step, already collapsed to 0 under reduce-motion. */
  duration: (step: MotionDurationStep) => number
}

const ThemeContext = createContext<Theme | null>(null)

/**
 * Tracks the OS "reduce motion" setting.
 *
 * `packages/ui` exposes `getMotion(prefersReducedMotion)` and documents that React
 * Native should feed it `AccessibilityInfo.isReduceMotionEnabled()` — this is that
 * feed. Kept local because the value is async and changes at runtime.
 */
function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false)
  useEffect(() => {
    let active = true
    AccessibilityInfo.isReduceMotionEnabled().then((value) => {
      if (active) setReduced(value)
    })
    const sub = AccessibilityInfo.addEventListener("reduceMotionChanged", setReduced)
    return () => {
      active = false
      sub.remove()
    }
  }, [])
  return reduced
}

export function ThemeProvider({ children }: { children: ReactNode }): JSX.Element {
  const systemScheme = useColorScheme()
  const { themeMode } = usePreferences()
  const prefersReducedMotion = useReducedMotion()

  const value = useMemo<Theme>(() => {
    const scheme: ColorScheme = themeMode === "system" ? (systemScheme === "light" ? "light" : "dark") : themeMode
    const palette = scheme === "light" ? lightPalette : darkPalette
    return {
      scheme,
      mode: themeMode,
      palette,
      // Dark accent (#8499d9) reads best against the brand navy; the light accent is
      // dark enough that the light surface colour is the right foreground.
      onAccent: scheme === "light" ? lightPalette.surface : brandNavy,
      radii,
      radiusPill,
      spacing,
      type: typeStyles,
      shadow: (level: ElevationLevel) => rnShadow(level, scheme),
      motion: motionRn,
      prefersReducedMotion,
      duration: (step: MotionDurationStep) => motionRn.duration(step, prefersReducedMotion),
    }
  }, [systemScheme, themeMode, prefersReducedMotion])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): Theme {
  const theme = useContext(ThemeContext)
  if (!theme) throw new Error("useTheme must be used inside <ThemeProvider>")
  return theme
}

/**
 * Builds a memoised StyleSheet from the active theme.
 *
 *   const styles = useThemedStyles((t) => ({ root: { backgroundColor: t.palette.bg } }))
 */
export function useThemedStyles<T extends Record<string, object>>(factory: (theme: Theme) => T): T {
  const theme = useTheme()
  return useMemo(() => factory(theme), [theme, factory])
}
