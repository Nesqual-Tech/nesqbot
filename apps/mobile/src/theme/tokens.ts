/**
 * React Native adapter for the `@nesqbot/ui` design tokens.
 *
 * `packages/ui` is deliberately platform-neutral: colours, spacing, radii and the
 * `elevation` token (which already carries `androidElevation`) are plain numbers and
 * strings that React Native consumes as-is, and `typeScale` was authored with absolute
 * `lineHeight` / `letterSpacing` precisely so that CSS and RN agree pixel for pixel.
 *
 * Exactly three things do NOT cross the boundary, and this file is the thin adapter for
 * them — NOT a fork. Every value still originates in `@nesqbot/ui`:
 *
 *   1. `fontFamily` — the tokens ship CSS font *stacks* (`'"Inter", "Segoe UI", ...'`).
 *      React Native takes a single family name and has no fallback list.
 *   2. `easing`     — the tokens ship CSS `cubic-bezier(...)` strings. RN wants an
 *      `Easing` function. We parse the control points out of the same string rather
 *      than re-typing the numbers, so the curve cannot drift from the web's.
 *   3. `boxShadow`  — the tokens ship a CSS shorthand. RN wants discrete
 *      `shadowColor` / `shadowOffset` / `shadowOpacity` / `shadowRadius` / `elevation`.
 *      We build those from the structured `elevation` token, not from the string.
 *
 * If you need a new value, add it to `packages/ui` and adapt it here. Never hardcode a
 * colour, size or duration in this app.
 */
import { Easing, Platform, type EasingFunction, type TextStyle, type ViewStyle } from "react-native"
import {
  durations,
  easings,
  elevation,
  typeScale,
  typography,
  type ColorScheme,
  type ElevationLevel,
  type MotionDurationStep,
  type MotionEasingStep,
  type TypeScaleStep,
} from "@nesqbot/ui"

/* ------------------------------------------------------------------ *
 * 1. Font families
 * ------------------------------------------------------------------ */

/**
 * The app does not bundle Inter or Poppins (there is no `expo-font` dependency), so
 * naming them here would be a lie: iOS would silently ignore an unknown family and
 * Android would fall back to Roboto, with nobody having chosen either.
 *
 * The honest mapping is therefore: the sans and display stacks resolve to the platform
 * system face (`undefined` — RN's documented way of saying "system default"), and only
 * the monospace stack, where the *shape* carries meaning, names a real installed face.
 *
 * This is a deliberate, documented parity gap with the desktop app. Closing it means
 * adding `expo-font` and bundling the two woff2/ttf files — see docs/mobile-parity.md.
 */
const MONO_FAMILY = Platform.select({ ios: "Menlo", android: "monospace", default: "monospace" })

/** Maps one of the CSS stacks in `typography` onto a React Native family name. */
export function fontFamilyFor(cssStack: string): string | undefined {
  if (cssStack === typography.fontMono) return MONO_FAMILY
  // fontSans, fontDisplay and fontBrand all resolve to the system face until the
  // brand faces are bundled. `undefined` is RN's "system default".
  return undefined
}

/* ------------------------------------------------------------------ *
 * 2. Type scale
 * ------------------------------------------------------------------ */

/**
 * A `typeScale` step as a React Native `TextStyle`.
 *
 * `fontSize`, `lineHeight`, `letterSpacing` and the numeric `fontWeight` all pass
 * straight through — RN accepts numeric weights. Only `fontFamily` is translated, and
 * `textTransform` is forwarded because RN's `Text` honours the same property name.
 */
export function typeStyle(step: TypeScaleStep): TextStyle {
  const token = typeScale[step]
  const style: TextStyle = {
    fontSize: token.fontSize,
    lineHeight: token.lineHeight,
    fontWeight: token.fontWeight,
    letterSpacing: token.letterSpacing,
  }
  const family = fontFamilyFor(token.fontFamily)
  if (family) style.fontFamily = family
  if (token.textTransform) style.textTransform = token.textTransform
  return style
}

/** Every `typeScale` step, pre-converted. Build once at module load; RN styles are static. */
export const type: Record<TypeScaleStep, TextStyle> = Object.fromEntries(
  (Object.keys(typeScale) as TypeScaleStep[]).map((step) => [step, typeStyle(step)]),
) as Record<TypeScaleStep, TextStyle>

/* ------------------------------------------------------------------ *
 * 3. Elevation / shadow
 * ------------------------------------------------------------------ */

/**
 * Shadows are cast in brand navy, not black — the same decision `packages/ui` makes for
 * CSS. The alpha scale (light mode gets a softer cast at 0.45) mirrors `shadows` in the
 * tokens package; it is applied here to the structured token instead of to a string.
 */
const SHADOW_COLOR = "#0b0d1a"
const LIGHT_SHADOW_SCALE = 0.45

/**
 * React Native shadow props for an elevation level.
 *
 * RN has no `spread`, so the token's negative spread — which on the web tightens a wide
 * blur — is folded into the radius. Android reads only `elevation`, which the token
 * already carries as `androidElevation`.
 */
export function shadow(level: ElevationLevel, scheme: ColorScheme = "dark"): ViewStyle {
  const token = elevation[level]
  if (token.opacity === 0) return {}
  const scale = scheme === "light" ? LIGHT_SHADOW_SCALE : 1
  return {
    shadowColor: SHADOW_COLOR,
    shadowOffset: { width: 0, height: token.offsetY },
    shadowOpacity: Math.min(1, Math.round(token.opacity * scale * 100) / 100),
    // RN's shadowRadius is roughly half the CSS blur; spread has no RN equivalent, so
    // fold it in rather than dropping it and rendering a visibly larger shadow.
    shadowRadius: Math.max(0, (token.blur + token.spread) / 2),
    elevation: token.androidElevation,
  }
}

/* ------------------------------------------------------------------ *
 * 4. Motion
 * ------------------------------------------------------------------ */

/**
 * Pulls the four control points out of a CSS `cubic-bezier(a, b, c, d)` string.
 *
 * Parsing rather than re-typing is the point: `packages/ui` stays the single definition
 * of every curve, and a change there reaches the phone without a second edit.
 */
function parseCubicBezier(css: string): [number, number, number, number] | null {
  const match = /^cubic-bezier\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)$/.exec(css)
  if (!match) return null
  const points = match.slice(1, 5).map(Number)
  if (points.some((n) => Number.isNaN(n))) return null
  return points as [number, number, number, number]
}

/** A `motion.easing` step as a React Native `EasingFunction`. */
export function easing(step: MotionEasingStep): EasingFunction {
  const css = easings[step]
  if (css === "linear") return Easing.linear
  const points = parseCubicBezier(css)
  if (!points) return Easing.linear
  return Easing.bezier(points[0], points[1], points[2], points[3])
}

/** Every easing step, pre-converted. */
export const easingFns: Record<MotionEasingStep, EasingFunction> = Object.fromEntries(
  (Object.keys(easings) as MotionEasingStep[]).map((step) => [step, easing(step)]),
) as Record<MotionEasingStep, EasingFunction>

/**
 * Duration in ms, collapsing to 0 when the OS asks for reduced motion.
 *
 * Mirrors `getMotion()` in the tokens package; kept as a function here because RN reads
 * the preference asynchronously via `AccessibilityInfo`.
 */
export function duration(step: MotionDurationStep, prefersReducedMotion = false): number {
  return prefersReducedMotion ? 0 : durations[step]
}

/** The RN-shaped motion token set. */
export interface RnMotion {
  duration: (step: MotionDurationStep, reduced?: boolean) => number
  easing: Record<MotionEasingStep, EasingFunction>
}

export const motionRn: RnMotion = { duration, easing: easingFns }
