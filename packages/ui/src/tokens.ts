/**
 * Nesqual Tech design tokens for Nesq Bot (desktop + mobile).
 *
 * Primitives only: brand colours, palettes, scales. Anything that maps a
 * token onto a *meaning* (risk classes, bot states) lives in `semantic.ts`;
 * anything that emits CSS lives in `css.ts`.
 *
 * Numbers are unitless so React Native can use them directly. The CSS emitter
 * appends `px` where a unit is required.
 */

import { BRAND_MARK_HEX } from "./brand"

/* ------------------------------------------------------------------ *
 * Brand
 * ------------------------------------------------------------------ */

/**
 * The mark colour, sampled from the live artwork (see `brand.ts`).
 *
 * Was `#8499d9` here until the PNG was actually measured. One step of blue,
 * but it was the anchor the whole ramp below was built around.
 */
export const logoInk = BRAND_MARK_HEX

/**
 * The brand ramp, rebuilt around the sampled anchor.
 *
 * Generated in OKLCH: hue locked to 270 (the anchor measures 269.97), even
 * perceptual lightness steps, and a chroma arc that peaks at 600 and falls
 * away at both ends so the pale tints do not look dirty and the dark shades do
 * not look like navy. Step 400 is `logoInk` exactly and is the only value that
 * is not free to move.
 *
 * The previous ramp was close — it was hand-picked around the right idea — but
 * its hue wandered between 268.3 and 272.9, its chroma peaked at 700 instead of
 * the middle, and its lightness steps were uneven (0.039 from 50 to 100 against
 * 0.082 from 300 to 400), which is why the light end looked flat.
 */
export const brandMark = {
  50: "#eff2fc",
  100: "#dde4f7",
  200: "#c0ccef",
  300: "#a1b2e6",
  400: BRAND_MARK_HEX,
  500: "#6a80c9",
  600: "#5469b3",
  700: "#435599",
  800: "#35457d",
  900: "#2a3662",
} as const

export const brandNavy = "#0b0d1a"

/**
 * The gradient family. Deliberately *not* the brand — the real identity is one
 * accent on white and dark, and these four are a UI device for distinguishing
 * bots from one another and for the one hero surface per screen. If you find
 * yourself reaching for `accentSweep` on a third component, the answer is
 * `brandMark` instead.
 */
export const brandGradient = {
  violet: "#7c3aed",
  fuchsia: "#c026d3",
  cyan: "#22d3ee",
  indigo: "#6366f1",
} as const

export const accentSweep = ["#7c3aed", "#c026d3", "#22d3ee"] as const

/* ------------------------------------------------------------------ *
 * Palettes
 * ------------------------------------------------------------------ */

export type ColorScheme = "dark" | "light"

export const COLOR_SCHEMES = ["dark", "light"] as const satisfies readonly ColorScheme[]

/**
 * The dark palette. This is the product's native scheme: the logo is drawn in
 * white plus one accent on transparency, so dark is where the brand is itself
 * and light is the alternative.
 *
 * Contrast rules the values below satisfy (measured, not asserted — run
 * `contrastAudit()` from `audit.ts`, or `node scripts/check-brand.mjs`):
 *
 *   - `text`, `textMuted`, `textDim` clear 4.5:1 against all four grounds
 *     (`bg`, `surface`, `surfaceAlt`, `surfaceRaised`).
 *   - `accent`, `accentStrong`, `danger`, `success`, `warning` clear 4.5:1
 *     against `bg` and `surface`, the two grounds text actually sits on.
 */
export const darkPalette = {
  bg: "#0b0d1a",
  surface: "#12152a",
  surfaceAlt: "#1a1e36",
  surfaceRaised: "#222744",
  surfacePressed: "#2a3050",
  border: "#2a3050",
  text: "#f2f4ff",
  textMuted: "#9aa3c7",
  // Was #6b7394, which measured 3.86:1 on `surface` and 3.12:1 on
  // `surfaceRaised` — below AA everywhere it was used as text.
  textDim: "#858eb0",
  accent: logoInk,
  // Was brandGradient.violet. On a dark ground that violet is *darker* than
  // `accent`, so "strong" pointed the wrong way and measured 3.39:1 on `bg`.
  // Same hue, lifted until it clears AA.
  accentStrong: "#915eff",
  danger: "#ef4444",
  success: "#22c55e",
  warning: "#f59e0b",
} as const

/**
 * The light palette.
 *
 * Not an inversion of the dark one. The dark scheme can lean on saturated mid
 * tones because everything sits on near-black; on a white ground the same
 * colours collapse — the old `success` (#16a34a) measured 3.05:1 and `warning`
 * (#d97706) 2.95:1 against `bg`. Both were re-derived in OKLCH: same hue, taken
 * down in lightness only until they clear 4.5:1.
 */
export const lightPalette = {
  bg: "#f5f6fb",
  surface: "#ffffff",
  surfaceAlt: "#eef0f8",
  surfaceRaised: "#ffffff",
  surfacePressed: "#e4e7f2",
  border: "#d5dae8",
  text: "#0b0d1a",
  textMuted: "#5a6280",
  // Was #8a92ad: 2.86:1 on `bg`, 3.09:1 on `surface`. Nowhere near AA.
  textDim: "#666d87",
  accent: brandMark[600],
  accentStrong: brandGradient.violet,
  danger: "#db2525", // was #dc2626 — 4.47:1 on `bg`, just short
  success: "#008338", // was #16a34a — 3.05:1 on `bg`
  warning: "#ac5d00", // was #d97706 — 2.95:1 on `bg`
} as const

export type PaletteRole = keyof typeof darkPalette

/**
 * The shape both palettes share. `darkPalette` defines the role set because it
 * is the default scheme; `lightPalette` is structurally identical. Values are
 * widened to `string` — the two palettes hold different literals, so a
 * literal-typed alias would make only one of them assignable.
 */
export type Palette = { readonly [K in PaletteRole]: string }

/** Pick a palette by scheme. Defaults to dark, the product's native mode. */
export function getPalette(scheme: ColorScheme = "dark"): Palette {
  return scheme === "light" ? lightPalette : darkPalette
}

/* ------------------------------------------------------------------ *
 * Shape & space
 * ------------------------------------------------------------------ */

export const radii = {
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
} as const

/** Fully rounded — pills and avatars. Kept out of `radii` so it stays finite. */
export const radiusPill = 999

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const

/* ------------------------------------------------------------------ *
 * Type
 * ------------------------------------------------------------------ */

/**
 * Font stacks.
 *
 * The logo is set in a wide geometric sans. The honest way to get closer to it
 * without adding a dependency or a network fetch is to name geometric faces
 * first and let them be no-ops when they are absent — Poppins and Montserrat
 * are not installed by default anywhere, so they only take effect if an app
 * deliberately bundles one.
 *
 * Century Gothic and Futura are the obvious other candidates and are
 * deliberately excluded: both ship with common Office / macOS installs, so
 * naming them would make the app render differently from machine to machine
 * with nobody having chosen it. Inter stays the workhorse; it is what the
 * desktop app already loads.
 */
export const typography = {
  fontSans: '"Inter", "Segoe UI", system-ui, sans-serif',
  fontDisplay: '"Poppins", "Montserrat", "Inter", "Segoe UI", system-ui, sans-serif',
  /** The wordmark face. Used by the SVG lockup in `logo.ts`. */
  fontBrand: '"Poppins", "Montserrat", "Inter", "Segoe UI", system-ui, sans-serif',
  fontMono: '"JetBrains Mono", "Cascadia Code", ui-monospace, monospace',
} as const

/**
 * Letter-spacing, as a fraction of font size.
 *
 * The logo's own tracking, measured off the artwork: the wordmark runs about
 * +0.06em and the tagline about +0.16em. Those two numbers are `wide` and
 * `widest`, and they are what makes an eyebrow read as Nesqual rather than as
 * generic small caps.
 *
 * `typeScale` carries absolute values because React Native has no em; use
 * `trackingPx` when you need to apply one of these to an arbitrary size.
 */
export const tracking = {
  tighter: -0.02,
  tight: -0.012,
  normal: 0,
  wide: 0.06,
  wider: 0.1,
  widest: 0.16,
} as const

export type TrackingStep = keyof typeof tracking

/** Absolute letter-spacing in px for a tracking step at a given font size. */
export function trackingPx(step: TrackingStep, fontSize: number): number {
  return Math.round(tracking[step] * fontSize * 100) / 100
}

export interface TypeStyle {
  fontSize: number
  lineHeight: number
  fontWeight: 300 | 400 | 500 | 600 | 700
  /** Absolute px, not em — React Native has no em. */
  letterSpacing: number
  fontFamily: string
  /**
   * Optional case transform. Honoured by CSS `text-transform` and by the React
   * Native `Text` style of the same name. Set only where the case is part of
   * the style rather than of the string, so the string stays readable in source
   * and in a screen reader.
   */
  textTransform?: "uppercase" | "none"
}

export type TypeScaleStep =
  | "displayLg"
  | "display"
  | "title"
  | "heading"
  | "subheading"
  | "body"
  | "bodyStrong"
  | "label"
  | "labelCaps"
  | "eyebrow"
  | "caption"
  | "mono"

/**
 * One scale for both platforms. Line heights are absolute so that React
 * Native (which has no unitless line-height) and CSS agree pixel for pixel.
 *
 * Tracking follows the logo: display sizes are pulled tight (the wordmark is
 * big and does not need air), and the uppercase steps — `labelCaps` and
 * `eyebrow` — are opened up to roughly the tagline's +0.16em, which is the most
 * recognisably Nesqual thing the type system can do. Values are absolute px;
 * `tracking` above has the em fractions they came from.
 */
export const typeScale: Record<TypeScaleStep, TypeStyle> = {
  displayLg: { fontSize: 44, lineHeight: 50, fontWeight: 700, letterSpacing: -1, fontFamily: typography.fontDisplay },
  display: { fontSize: 32, lineHeight: 38, fontWeight: 700, letterSpacing: -0.6, fontFamily: typography.fontDisplay },
  title: { fontSize: 24, lineHeight: 30, fontWeight: 600, letterSpacing: -0.4, fontFamily: typography.fontDisplay },
  heading: { fontSize: 18, lineHeight: 24, fontWeight: 600, letterSpacing: -0.2, fontFamily: typography.fontSans },
  subheading: { fontSize: 15, lineHeight: 20, fontWeight: 600, letterSpacing: 0, fontFamily: typography.fontSans },
  body: { fontSize: 14, lineHeight: 21, fontWeight: 400, letterSpacing: 0, fontFamily: typography.fontSans },
  bodyStrong: { fontSize: 14, lineHeight: 21, fontWeight: 600, letterSpacing: 0, fontFamily: typography.fontSans },
  label: { fontSize: 12, lineHeight: 16, fontWeight: 600, letterSpacing: 0.3, fontFamily: typography.fontSans },
  labelCaps: {
    fontSize: 12,
    lineHeight: 16,
    fontWeight: 600,
    letterSpacing: 1.2,
    fontFamily: typography.fontSans,
    textTransform: "uppercase",
  },
  eyebrow: {
    fontSize: 11,
    lineHeight: 14,
    fontWeight: 700,
    letterSpacing: 1.8,
    fontFamily: typography.fontDisplay,
    textTransform: "uppercase",
  },
  caption: { fontSize: 11, lineHeight: 15, fontWeight: 400, letterSpacing: 0.2, fontFamily: typography.fontSans },
  mono: { fontSize: 12.5, lineHeight: 19, fontWeight: 400, letterSpacing: 0, fontFamily: typography.fontMono },
}

/* ------------------------------------------------------------------ *
 * Elevation
 * ------------------------------------------------------------------ */

export type ElevationLevel = "none" | "raised" | "overlay" | "popover" | "modal"

export const ELEVATION_LEVELS = [
  "none",
  "raised",
  "overlay",
  "popover",
  "modal",
] as const satisfies readonly ElevationLevel[]

/** Platform-neutral description of a shadow. */
export interface ElevationToken {
  /** Vertical offset in px. */
  offsetY: number
  /** Blur radius in px. */
  blur: number
  /** Spread in px. */
  spread: number
  /** Shadow alpha, 0–1. Dark surfaces need a heavier shadow to read. */
  opacity: number
  /** Android `elevation` equivalent, for React Native. */
  androidElevation: number
}

export const elevation: Record<ElevationLevel, ElevationToken> = {
  none: { offsetY: 0, blur: 0, spread: 0, opacity: 0, androidElevation: 0 },
  raised: { offsetY: 1, blur: 2, spread: 0, opacity: 0.18, androidElevation: 1 },
  overlay: { offsetY: 4, blur: 12, spread: -2, opacity: 0.28, androidElevation: 4 },
  popover: { offsetY: 8, blur: 24, spread: -4, opacity: 0.36, androidElevation: 8 },
  modal: { offsetY: 16, blur: 48, spread: -8, opacity: 0.48, androidElevation: 16 },
}

/** Shadows are cast in brand navy, not black — black reads muddy on #0b0d1a. */
const SHADOW_RGB = "11, 13, 26"

function boxShadow(token: ElevationToken, scale: number): string {
  if (token.opacity === 0) return "none"
  const alpha = Math.min(1, Math.round(token.opacity * scale * 100) / 100)
  return `0 ${token.offsetY}px ${token.blur}px ${token.spread}px rgba(${SHADOW_RGB}, ${alpha})`
}

/** Ready-made CSS `box-shadow` values. Light mode needs a softer cast. */
export const shadows: Record<ColorScheme, Record<ElevationLevel, string>> = {
  dark: {
    none: boxShadow(elevation.none, 1),
    raised: boxShadow(elevation.raised, 1),
    overlay: boxShadow(elevation.overlay, 1),
    popover: boxShadow(elevation.popover, 1),
    modal: boxShadow(elevation.modal, 1),
  },
  light: {
    none: boxShadow(elevation.none, 0.45),
    raised: boxShadow(elevation.raised, 0.45),
    overlay: boxShadow(elevation.overlay, 0.45),
    popover: boxShadow(elevation.popover, 0.45),
    modal: boxShadow(elevation.modal, 0.45),
  },
}

export function getShadow(level: ElevationLevel, scheme: ColorScheme = "dark"): string {
  return shadows[scheme][level]
}

/* ------------------------------------------------------------------ *
 * Motion
 * ------------------------------------------------------------------ */

export type MotionDurationStep = "instant" | "fast" | "base" | "slow" | "deliberate"

/** Milliseconds. */
export const durations: Record<MotionDurationStep, number> = {
  instant: 0,
  fast: 120,
  base: 200,
  slow: 320,
  deliberate: 480,
}

export type MotionEasingStep = "linear" | "standard" | "entrance" | "exit" | "emphasized"

export const easings: Record<MotionEasingStep, string> = {
  linear: "linear",
  /** Default for state changes that stay on screen. */
  standard: "cubic-bezier(0.2, 0, 0, 1)",
  /** Things arriving: fast out of the gate, gentle landing. */
  entrance: "cubic-bezier(0.05, 0.7, 0.1, 1)",
  /** Things leaving: no anticipation, get out of the way. */
  exit: "cubic-bezier(0.3, 0, 0.8, 0.15)",
  /** Reserved for the one hero movement per screen. */
  emphasized: "cubic-bezier(0.3, 0, 0, 1.2)",
}

export interface MotionTokens {
  duration: Record<MotionDurationStep, number>
  easing: Record<MotionEasingStep, string>
  /** True when this token set is the reduced-motion variant. */
  reduced: boolean
}

export const motion: MotionTokens = {
  duration: durations,
  easing: easings,
  reduced: false,
}

/**
 * Reduced-motion variant. Durations collapse to zero and every curve becomes
 * linear, so a component can keep its transition wiring and simply swap the
 * token set instead of branching on every animated property.
 */
export const reducedMotion: MotionTokens = {
  duration: { instant: 0, fast: 0, base: 0, slow: 0, deliberate: 0 },
  easing: { linear: "linear", standard: "linear", entrance: "linear", exit: "linear", emphasized: "linear" },
  reduced: true,
}

/**
 * Pick the right token set.
 *
 * Pass the result of `matchMedia("(prefers-reduced-motion: reduce)").matches`
 * on web, or `AccessibilityInfo.isReduceMotionEnabled()` on React Native.
 */
export function getMotion(prefersReducedMotion = false): MotionTokens {
  return prefersReducedMotion ? reducedMotion : motion
}

/** `"200ms cubic-bezier(...)"` — the tail of a CSS `transition` shorthand. */
export function transition(
  duration: MotionDurationStep = "base",
  easing: MotionEasingStep = "standard",
  tokens: MotionTokens = motion,
): string {
  return `${tokens.duration[duration]}ms ${tokens.easing[easing]}`
}

/* ------------------------------------------------------------------ *
 * Bot identity colours
 * ------------------------------------------------------------------ */

/** Per-bot accent, keyed by bot slug. Unknown slugs fall back to `custom`. */
export const botColors: Record<string, string> = {
  chief_of_staff: logoInk,
  lead_generator: brandGradient.cyan,
  sales: brandGradient.violet,
  ops: brandGradient.indigo,
  support: brandGradient.fuchsia,
  custom: brandMark[500],
}

export function getBotColor(slug: string): string {
  return botColors[slug] ?? botColors["custom"] ?? brandMark[500]
}

/**
 * Per-bot silhouette, keyed by bot slug.
 *
 * Colour alone was doing all the identifying, and a column of tinted circles
 * with two-letter initials in them reads as one control repeated rather than
 * as five different teammates — worse still for the two bots whose names both
 * start with S. A shape is legible at 20px, survives greyscale and colour
 * blindness, and is the thing people actually end up naming ("the triangle
 * one").
 *
 * The mapping is a name, not a path: the shape has to be drawn with SVG on the
 * desktop and `react-native-svg` on mobile, and the one thing both need to
 * agree on is which bot gets which. Unknown slugs are assigned deterministically
 * from the name — see `getBotShape` — so a custom bot keeps its shape across
 * launches without storing anything.
 */
export type BotShape = "hexagon" | "triangle" | "square" | "cloud" | "circle"

export const botShapes: Record<string, BotShape> = {
  chief_of_staff: "hexagon",
  lead_generator: "triangle",
  sales: "square",
  support: "cloud",
  ops: "circle",
}

const SHAPE_CYCLE: BotShape[] = ["hexagon", "triangle", "square", "cloud", "circle"]

export function getBotShape(slug: string): BotShape {
  const known = botShapes[slug]
  if (known) return known
  // A stable hash rather than a counter: the sidebar sorts and re-sorts, and a
  // teammate whose shape changes when a bot is added above them is worse than
  // no shape at all.
  let hash = 0
  for (let index = 0; index < slug.length; index += 1) {
    hash = (hash * 31 + slug.charCodeAt(index)) | 0
  }
  return SHAPE_CYCLE[Math.abs(hash) % SHAPE_CYCLE.length]
}
