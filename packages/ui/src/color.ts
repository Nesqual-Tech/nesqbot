/**
 * Colour maths.
 *
 * Pure functions over `#rrggbb` strings — no tokens, no DOM, no dependencies,
 * so React Native can call them too. Everything the palette layer needs in
 * order to *measure* contrast rather than assert it lives here.
 *
 * Two colour spaces are used and they are not interchangeable:
 *
 *   - WCAG relative luminance, for contrast ratios. This is the only thing
 *     that decides whether text is legible.
 *   - OKLCH, for moving a colour without changing what it *is*. Lightening a
 *     brand colour in sRGB drags its hue; in OKLCH it does not.
 */

/* ------------------------------------------------------------------ *
 * Hex <-> RGB
 * ------------------------------------------------------------------ */

export type Rgb = readonly [number, number, number]

/**
 * Parse `#rgb`, `#rrggbb` or `#rrggbbaa`. Any alpha is discarded — contrast is
 * only meaningful between two opaque colours, and a caller that composites a
 * translucent layer must do so before asking.
 */
export function hexToRgb(hex: string): Rgb {
  const raw = hex.startsWith("#") ? hex.slice(1) : hex
  const full = raw.length === 3 || raw.length === 4 ? raw.slice(0, 3).replace(/./g, (c) => c + c) : raw.slice(0, 6)
  const int = Number.parseInt(full, 16)
  if (full.length !== 6 || Number.isNaN(int)) return [0, 0, 0]
  return [(int >> 16) & 255, (int >> 8) & 255, int & 255]
}

export function rgbToHex(rgb: Rgb): string {
  const part = (v: number): string =>
    Math.max(0, Math.min(255, Math.round(v)))
      .toString(16)
      .padStart(2, "0")
  return `#${part(rgb[0])}${part(rgb[1])}${part(rgb[2])}`
}

/* ------------------------------------------------------------------ *
 * WCAG contrast
 * ------------------------------------------------------------------ */

function toLinear(channel: number): number {
  const c = channel / 255
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

function fromLinear(channel: number): number {
  return 255 * (channel <= 0.0031308 ? 12.92 * channel : 1.055 * channel ** (1 / 2.4) - 0.055)
}

/** WCAG 2.x relative luminance, 0 (black) to 1 (white). */
export function relativeLuminance(hex: string): number {
  const [r, g, b] = hexToRgb(hex)
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b)
}

/** WCAG contrast ratio between two colours, 1–21. Order does not matter. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/** Contrast ratio rounded to two decimals — what you want when reporting. */
export function contrastRatioRounded(a: string, b: string): number {
  return Math.round(contrastRatio(a, b) * 100) / 100
}

export type ContrastLevel = "AA" | "AAA"
export type TextSize = "body" | "large"

/**
 * WCAG thresholds. `large` means at least 18.66px bold or 24px regular, which
 * in this type scale is only `displayLg`, `display` and `title`.
 */
export const CONTRAST_THRESHOLDS: Record<ContrastLevel, Record<TextSize, number>> = {
  AA: { body: 4.5, large: 3 },
  AAA: { body: 7, large: 4.5 },
}

/** Non-text contrast minimum (WCAG 1.4.11): icons, dots, focus rings, hairlines. */
export const NON_TEXT_MIN = 3

export function meetsContrast(fg: string, bg: string, level: ContrastLevel = "AA", size: TextSize = "body"): boolean {
  return contrastRatio(fg, bg) >= CONTRAST_THRESHOLDS[level][size]
}

/**
 * Pick whichever candidate reads best on `background`, measured — not guessed
 * from a luma threshold. Returns the first candidate on a tie so the result is
 * stable across runs.
 */
export function readableOn(background: string, candidates: readonly string[] = ["#ffffff", "#000000"]): string {
  let best = candidates[0] ?? "#ffffff"
  let bestRatio = -1
  for (const candidate of candidates) {
    const ratio = contrastRatio(candidate, background)
    if (ratio > bestRatio) {
      best = candidate
      bestRatio = ratio
    }
  }
  return best
}

/* ------------------------------------------------------------------ *
 * OKLCH
 * ------------------------------------------------------------------ */

export interface Oklch {
  /** Perceptual lightness, 0–1. */
  L: number
  /** Chroma. 0 is grey; sRGB tops out around 0.37. */
  C: number
  /** Hue angle in degrees, 0–360. */
  h: number
}

export function hexToOklch(hex: string): Oklch {
  const [r8, g8, b8] = hexToRgb(hex)
  const r = toLinear(r8)
  const g = toLinear(g8)
  const b = toLinear(b8)
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  const okL = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s
  const okA = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s
  const okB = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s
  return { L: okL, C: Math.hypot(okA, okB), h: ((Math.atan2(okB, okA) * 180) / Math.PI + 360) % 360 }
}

function oklchToRgbRaw({ L, C, h }: Oklch): Rgb {
  const rad = (h * Math.PI) / 180
  const a = C * Math.cos(rad)
  const bb = C * Math.sin(rad)
  const l = (L + 0.3963377774 * a + 0.2158037573 * bb) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * bb) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * bb) ** 3
  return [
    fromLinear(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    fromLinear(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    fromLinear(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

function inSrgb(rgb: Rgb): boolean {
  return rgb.every((c) => c >= -0.5 && c <= 255.5)
}

/**
 * OKLCH to `#rrggbb`, reducing chroma until the colour fits sRGB.
 *
 * Clipping the channels instead would shift hue and lightness — the two things
 * the whole exercise is trying to preserve — so chroma is the one that gives.
 */
export function oklchToHex(oklch: Oklch): string {
  const direct = oklchToRgbRaw(oklch)
  if (inSrgb(direct)) return rgbToHex(direct)
  let lo = 0
  let hi = oklch.C
  for (let i = 0; i < 24; i++) {
    const mid = (lo + hi) / 2
    if (inSrgb(oklchToRgbRaw({ ...oklch, C: mid }))) lo = mid
    else hi = mid
  }
  return rgbToHex(oklchToRgbRaw({ ...oklch, C: lo }))
}

/** Move a colour's OKLCH lightness, keeping hue and (where sRGB allows) chroma. */
export function withLightness(hex: string, L: number): string {
  return oklchToHex({ ...hexToOklch(hex), L: Math.max(0, Math.min(1, L)) })
}

/* ------------------------------------------------------------------ *
 * Contrast repair
 * ------------------------------------------------------------------ */

/**
 * Return `hex` if it already clears `target` against every ground, otherwise
 * the nearest colour of the same hue that does.
 *
 * Only lightness moves, and only away from the grounds, so the result still
 * reads as the same colour — a legible red is still red. Bisection converges
 * in 20 steps; contrast is monotonic in lightness on one side of a ground,
 * which is the only case this is used for. If no lightness reaches the target,
 * the best attempt is returned rather than throwing — `contrastAudit()` in
 * `audit.ts` is what tells you it fell short.
 */
export function adjustToContrast(hex: string, grounds: readonly string[], target: number): string {
  if (grounds.length === 0) return hex
  const worst = (candidate: string): number => Math.min(...grounds.map((g) => contrastRatio(candidate, g)))
  if (worst(hex) >= target) return hex

  // Lighten on dark grounds, darken on light ones.
  const groundLuminance = grounds.reduce((sum, g) => sum + relativeLuminance(g), 0) / grounds.length
  const limit = groundLuminance < 0.18 ? 1 : 0
  const base = hexToOklch(hex)

  const extreme = oklchToHex({ ...base, L: limit })
  if (worst(extreme) < target) return extreme

  let lo = base.L
  let hi = limit
  for (let i = 0; i < 20; i++) {
    const mid = (lo + hi) / 2
    if (worst(oklchToHex({ ...base, L: mid })) >= target) hi = mid
    else lo = mid
  }
  return oklchToHex({ ...base, L: hi })
}
