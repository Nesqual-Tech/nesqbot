/**
 * CSS custom-property emission.
 *
 * The desktop app injects one of these strings into a `<style>` element at
 * boot; mobile ignores this file entirely and consumes the raw tokens.
 */

import {
  accentSweep,
  brandGradient,
  brandMark,
  brandNavy,
  durations,
  easings,
  getPalette,
  logoInk,
  radii,
  radiusPill,
  shadows,
  spacing,
  tracking,
  typeScale,
  typography,
  type ColorScheme,
  type ElevationLevel,
} from "./tokens"
import { BOT_STATE_TOKENS, RISK_CLASSES, botStateColors, riskColors, statusColors, type StatusRole } from "./semantic"

export interface CssVariablesOptions {
  /** Selector the block is written under. Default `:root`. */
  selector?: string
  /**
   * Emit a `@media (prefers-reduced-motion: reduce)` block that zeroes every
   * `--duration-*`. Default true — opt out only if you emit your own.
   */
  includeReducedMotion?: boolean
}

const STATUS_ROLES: readonly StatusRole[] = ["info", "success", "warning", "danger", "neutral"]
const ELEVATION_LEVELS_ORDER: readonly ElevationLevel[] = ["none", "raised", "overlay", "popover", "modal"]

function buildCssVariables(scheme: ColorScheme, options: CssVariablesOptions = {}): string {
  const selector = options.selector ?? ":root"
  const includeReducedMotion = options.includeReducedMotion ?? true
  const palette = getPalette(scheme)
  const risk = riskColors[scheme]
  const states = botStateColors[scheme]
  const status = statusColors[scheme]
  const shadow = shadows[scheme]
  const lines: string[] = []

  const push = (name: string, value: string | number): void => {
    lines.push(`  --${name}: ${value};`)
  }

  push("color-scheme", scheme)

  // Brand
  push("brand-mark", logoInk)
  for (const [step, value] of Object.entries(brandMark)) push(`brand-mark-${step}`, value)
  push("brand-navy", brandNavy)
  push("brand-violet", brandGradient.violet)
  push("brand-fuchsia", brandGradient.fuchsia)
  push("brand-cyan", brandGradient.cyan)
  push("brand-indigo", brandGradient.indigo)
  push("accent-sweep", `linear-gradient(90deg, ${accentSweep[0]}, ${accentSweep[1]} 50%, ${accentSweep[2]})`)

  // Surfaces & text
  push("bg", palette.bg)
  push("surface", palette.surface)
  push("surface-alt", palette.surfaceAlt)
  push("surface-raised", palette.surfaceRaised)
  push("surface-pressed", palette.surfacePressed)
  push("border", palette.border)
  push("text", palette.text)
  push("text-muted", palette.textMuted)
  push("text-dim", palette.textDim)
  push("accent", palette.accent)
  push("accent-strong", palette.accentStrong)
  push("danger", palette.danger)
  push("success", palette.success)
  push("warning", palette.warning)

  // Shape & space
  for (const [key, value] of Object.entries(radii)) push(`radius-${key}`, `${value}px`)
  push("radius-pill", `${radiusPill}px`)
  for (const [key, value] of Object.entries(spacing)) push(`space-${key}`, `${value}px`)

  // Type
  push("font-sans", typography.fontSans)
  push("font-display", typography.fontDisplay)
  push("font-brand", typography.fontBrand)
  push("font-mono", typography.fontMono)
  for (const [key, value] of Object.entries(tracking)) push(`tracking-${kebab(key)}`, `${value}em`)
  for (const [key, style] of Object.entries(typeScale)) {
    const name = kebab(key)
    push(`text-${name}-size`, `${style.fontSize}px`)
    push(`text-${name}-line`, `${style.lineHeight}px`)
    push(`text-${name}-weight`, style.fontWeight)
    // Letter-spacing was missing from the emitted set, so the web app silently
    // dropped every tracking value in the scale — including the wide uppercase
    // steps that are the most brand-specific thing in it.
    push(`text-${name}-tracking`, `${style.letterSpacing}px`)
    if (style.textTransform !== undefined) push(`text-${name}-transform`, style.textTransform)
  }

  // Elevation
  for (const level of ELEVATION_LEVELS_ORDER) push(`shadow-${level}`, shadow[level])

  // Motion
  for (const [key, value] of Object.entries(durations)) push(`duration-${key}`, `${value}ms`)
  for (const [key, value] of Object.entries(easings)) push(`ease-${key}`, value)

  // Semantic — risk classes
  for (const cls of RISK_CLASSES) {
    const r = risk[cls]
    push(`risk-${cls}`, r.solid)
    push(`risk-${cls}-fg`, r.fg)
    push(`risk-${cls}-bg`, r.bg)
    push(`risk-${cls}-border`, r.border)
    push(`risk-${cls}-on`, r.onSolid)
  }

  // Semantic — bot states
  for (const token of BOT_STATE_TOKENS) {
    const s = states[token]
    push(`bot-${kebab(token)}`, s.solid)
    // `-fg` is the same state colour corrected for text contrast; the bare
    // variable stays the dot fill. They are no longer the same value.
    push(`bot-${kebab(token)}-fg`, s.fg)
    push(`bot-${kebab(token)}-bg`, s.bg)
  }

  // Semantic — generic status
  for (const key of STATUS_ROLES) {
    const s = status[key]
    push(`status-${key}`, s.solid)
    push(`status-${key}-fg`, s.fg)
    push(`status-${key}-bg`, s.bg)
    push(`status-${key}-border`, s.border)
  }

  const block = `${selector} {\n${lines.join("\n")}\n}\n`
  if (!includeReducedMotion) return `\n${block}`

  const zeroed = Object.keys(durations)
    .map((key) => `    --duration-${key}: 0ms;`)
    .join("\n")
  return `\n${block}\n@media (prefers-reduced-motion: reduce) {\n  ${selector} {\n${zeroed}\n  }\n}\n`
}

function kebab(value: string): string {
  return value
    .replace(/_/g, "-")
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .toLowerCase()
}

/** The dark-scheme block. Nesq Bot's default. */
export const darkCssVariables: string = buildCssVariables("dark")

/** The light-scheme block. */
export const lightCssVariables: string = buildCssVariables("light")

/** Emit the custom-property block for either scheme. */
export function cssVariablesFor(scheme: ColorScheme = "dark", options: CssVariablesOptions = {}): string {
  if (options.selector === undefined && options.includeReducedMotion === undefined) {
    return scheme === "light" ? lightCssVariables : darkCssVariables
  }
  return buildCssVariables(scheme, options)
}

/**
 * `cssVariables` is both a string and a function.
 *
 * It shipped as a plain string (`style.textContent = cssVariables`) and the
 * desktop app still uses it that way, but a two-theme design system needs to
 * emit either palette. Renaming would have broken a live consumer, so the
 * export is a callable that coerces to the dark-scheme block: every string
 * operation — template literals, `String()`, `.includes()`, assignment to
 * `textContent` — behaves exactly as before, and `cssVariables("light")`
 * returns the light block.
 *
 * Two things it is not: a `<style>{cssVariables}</style>` JSX child (React
 * rejects function children) and a `JSON.stringify` input (functions
 * serialise to `undefined`). Use `cssVariablesFor(scheme)` in both cases.
 */
export type CssVariables = string & ((scheme?: ColorScheme, options?: CssVariablesOptions) => string)

function createCssVariables(): CssVariables {
  const base = darkCssVariables
  const callable = (scheme: ColorScheme = "dark", options: CssVariablesOptions = {}): string =>
    cssVariablesFor(scheme, options)

  const target = callable as unknown as Record<PropertyKey, unknown>

  // String coercion: `String(x)`, `` `${x}` ``, `x + ""`, `el.textContent = x`.
  target["toString"] = (): string => base
  target["valueOf"] = (): string => base
  target[Symbol.toPrimitive] = (): string => base
  target[Symbol.iterator] = () => base[Symbol.iterator]()

  // String instance methods, bound to the dark block, so `.startsWith()`,
  // `.includes()`, `.replace()` and friends keep working on the export.
  const proto = String.prototype as unknown as Record<string, unknown>
  for (const name of Object.getOwnPropertyNames(String.prototype)) {
    if (name === "constructor" || name === "length" || name === "toString" || name === "valueOf") {
      continue
    }
    const method = proto[name]
    if (typeof method !== "function") continue
    target[name] = (...args: unknown[]): unknown => (method as (...a: unknown[]) => unknown).apply(base, args)
  }

  // `.length` is a non-writable own property of every function; redefine it.
  Object.defineProperty(callable, "length", { value: base.length, configurable: true })

  return callable as unknown as CssVariables
}

export const cssVariables: CssVariables = createCssVariables()
