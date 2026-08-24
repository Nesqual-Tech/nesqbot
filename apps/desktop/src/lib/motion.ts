/**
 * The bridge between the design system's motion tokens and GSAP.
 *
 * ## Why this file exists at all
 *
 * `packages/ui` already owns motion. It emits `--duration-instant|fast|base|
 * slow|deliberate` and `--ease-linear|standard|entrance|exit|emphasized`, and
 * `cssVariablesFor` wraps the duration set in a
 * `@media (prefers-reduced-motion: reduce)` block that sets every one of them
 * to `0ms`. CSS transitions and keyframes therefore honour the preference for
 * free, which is how `styles.css` has always got it right.
 *
 * GSAP does not read CSS. Its durations are JavaScript numbers, so a naive
 * `gsap.to(el, { duration: 0.3 })` would sail straight past the design
 * system's reduced-motion block and animate anyway — the exact "bolted on
 * afterwards" failure the accessibility requirement is about.
 *
 * So every GSAP duration in this app comes from `dur()`, which reads the real
 * custom property off `:root`. Under reduced motion the token is already
 * `0ms`, so the tween is already instant. There is no second code path to keep
 * in sync and no `if (reduced)` sprinkled through the components for the
 * common case: the same cascade that clamps the CSS clamps the JavaScript.
 *
 * Three things the duration tokens cannot clamp on their own, because they are
 * not durations — `stagger`, `delay` and `repeat: -1`. Those get the explicit
 * helpers below. A zero-duration tween repeating forever is not "reduced
 * motion", it is a busy loop, so `loop()` exists and is not optional.
 *
 * ## Easing
 *
 * The tokens are `cubic-bezier()` strings and GSAP's core eases are named
 * curves, so the two would only ever be approximately the same shape. Rather
 * than eyeball a mapping onto `power2.out`, the token values are parsed and
 * registered with `CustomEase` under `nesq-*` names. The motion in JavaScript
 * is then literally the same curve as the motion in CSS, which matters most in
 * the places where both are on screen at once — a GSAP-entering toast sitting
 * next to a CSS-transitioning button.
 */
import { gsap } from "gsap"
import { CustomEase } from "gsap/CustomEase"
import { useGSAP } from "@gsap/react"
import { useEffect, useState } from "react"

gsap.registerPlugin(useGSAP, CustomEase)

export { gsap, useGSAP }

/* ------------------------------------------------------------------ *
 * Durations
 * ------------------------------------------------------------------ */

export type MotionStep = "instant" | "fast" | "base" | "slow" | "deliberate"

/**
 * Fallbacks, matching `durations` in `packages/ui/src/tokens.ts`.
 *
 * Only reachable before the token stylesheet has parsed, or in a test
 * environment with no CSSOM. Kept in seconds because that is GSAP's unit.
 */
const FALLBACK: Record<MotionStep, number> = {
  instant: 0,
  fast: 0.12,
  base: 0.2,
  slow: 0.32,
  deliberate: 0.48,
}

/**
 * A CSS time token, in seconds.
 *
 * Reads the unit rather than assuming one, and that is not defensiveness — it
 * is a bug that shipped. `css.ts` authors these as `200ms`, so the obvious
 * `parseFloat(raw) / 1000` looks right. But this reads them back through
 * `getComputedStyle`, and Chromium hands custom-property time values back
 * normalised to seconds: `.2s`, not `200ms`. Dividing that by 1000 made every
 * GSAP duration in the app 1000x too short -- 0.00032s instead of 0.32s -- so
 * every animation completed inside a single frame and the whole motion layer
 * was invisible while looking, in a screenshot, exactly like it had worked.
 */
function parseSeconds(raw: string): number | null {
  const value = Number.parseFloat(raw)
  if (!Number.isFinite(value)) return null
  // Order matters: "200ms" also ends with "s".
  if (raw.endsWith("ms")) return value / 1000
  if (raw.endsWith("s")) return value
  // Unitless. CSS would reject it as a <time>, so treat it as the authored
  // milliseconds rather than silently animating for 200 seconds.
  return value / 1000
}

let durationCache: Record<MotionStep, number> | null = null

function readDurations(): Record<MotionStep, number> {
  if (typeof document === "undefined") return FALLBACK
  const computed = getComputedStyle(document.documentElement)
  const step = (name: MotionStep): number => {
    const raw = computed.getPropertyValue(`--duration-${name}`).trim()
    if (!raw) return FALLBACK[name]
    return parseSeconds(raw) ?? FALLBACK[name]
  }
  return {
    instant: 0,
    fast: step("fast"),
    base: step("base"),
    slow: step("slow"),
    deliberate: step("deliberate"),
  }
}

/**
 * A duration token, in seconds, ready for a GSAP `duration`.
 *
 * Returns 0 under `prefers-reduced-motion: reduce`, because the token itself
 * is `0ms` there. Nothing at the call site has to know that.
 */
export function dur(step: MotionStep = "base"): number {
  durationCache ??= readDurations()
  return durationCache[step]
}

/* ------------------------------------------------------------------ *
 * Reduced motion
 * ------------------------------------------------------------------ */

const REDUCE_QUERY = "(prefers-reduced-motion: reduce)"

function reduceMediaQuery(): MediaQueryList | null {
  return typeof matchMedia === "function" ? matchMedia(REDUCE_QUERY) : null
}

export function prefersReducedMotion(): boolean {
  return reduceMediaQuery()?.matches ?? false
}

/**
 * `stagger` and `delay` are offsets, not durations, so the token block does
 * not zero them. Route both through here.
 */
export function stagger(seconds: number): number {
  return prefersReducedMotion() ? 0 : seconds
}

export const delay = stagger

/**
 * Guard for anything that repeats forever.
 *
 * Not a nicety. Under reduced motion every duration token is `0ms`, so a
 * `repeat: -1` tween would complete instantly and restart instantly, forever —
 * a zero-length loop spinning the ticker rather than an animation. Every
 * infinite tween in this app is wrapped in `if (loop())`.
 */
export function loop(): boolean {
  return !prefersReducedMotion()
}

/**
 * Re-render when the preference changes, so a `useGSAP` keyed on it rebuilds.
 *
 * Somebody turning reduced motion on in Windows Settings while the app is open
 * should not have to restart it to be believed.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion)

  useEffect(() => {
    const query = reduceMediaQuery()
    if (!query) return
    const onChange = (event: MediaQueryListEvent) => {
      // The duration tokens have just been re-resolved by the cascade; drop the
      // cache so the next `dur()` reads the new values rather than the old.
      durationCache = null
      setReduced(event.matches)
    }
    query.addEventListener("change", onChange)
    return () => query.removeEventListener("change", onChange)
  }, [])

  return reduced
}

/* ------------------------------------------------------------------ *
 * Easing
 * ------------------------------------------------------------------ */

export type MotionEase = "standard" | "entrance" | "exit" | "emphasized"

/** Fallbacks, matching `easings` in `packages/ui/src/tokens.ts`. */
const EASE_FALLBACK: Record<MotionEase, string> = {
  standard: "0.2,0,0,1",
  entrance: "0.05,0.7,0.1,1",
  exit: "0.3,0,0.8,0.15",
  emphasized: "0.3,0,0,1.2",
}

let easesRegistered = false

/** `cubic-bezier(0.2, 0, 0, 1)` -> `0.2,0,0,1`, which is CustomEase's format. */
function bezierPoints(value: string): string | null {
  const match = /cubic-bezier\(([^)]+)\)/.exec(value)
  if (!match) return null
  const points = match[1]
    .split(",")
    .map((part) => Number.parseFloat(part.trim()))
    .filter((part) => Number.isFinite(part))
  return points.length === 4 ? points.join(",") : null
}

function registerEases(): void {
  if (easesRegistered) return
  easesRegistered = true
  const computed = typeof document === "undefined" ? null : getComputedStyle(document.documentElement)
  for (const name of Object.keys(EASE_FALLBACK) as MotionEase[]) {
    const token = computed?.getPropertyValue(`--ease-${name}`).trim() ?? ""
    CustomEase.create(`nesq-${name}`, bezierPoints(token) ?? EASE_FALLBACK[name])
  }
}

/**
 * The GSAP name for a design-system easing token.
 *
 * Deliberately not clamped under reduced motion. The emitted reduced-motion
 * block only zeroes durations — an easing curve applied over zero seconds is
 * already a no-op, and leaving the curves alone means one less thing that
 * behaves differently between the two modes.
 */
export function ease(name: MotionEase = "standard"): string {
  registerEases()
  return `nesq-${name}`
}

/* ------------------------------------------------------------------ *
 * Shared defaults
 * ------------------------------------------------------------------ */

/**
 * The house style for an arrival: up from just below, no overshoot.
 *
 * `emphasized` is the only token curve that overshoots and the design system
 * reserves it for one hero movement per screen. In this app that movement is
 * the takeover badge and nothing else.
 */
export const ARRIVE = { y: 8, autoAlpha: 0 } as const

/** Called once, from `main.tsx`, so the eases exist before the first tween. */
export function initMotion(): void {
  registerEases()
  gsap.defaults({ ease: ease("standard"), duration: dur("base") })
}
