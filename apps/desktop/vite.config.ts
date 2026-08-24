import { defineConfig, type Plugin } from "vite"
import react from "@vitejs/plugin-react"
import path from "node:path"
import { cssVariablesFor } from "../../packages/ui/src/css"
import { brandNavy, darkPalette, durations, lightPalette, type Palette } from "../../packages/ui/src/tokens"

/* ------------------------------------------------------------------ *
 * Design tokens, as a real stylesheet
 * ------------------------------------------------------------------ */

/**
 * Why this exists — and it is the whole reason the packaged app used to look
 * broken.
 *
 * The app used to build the token block at runtime and push it into a
 * `<style>` element created with `document.createElement`. That works in `vite
 * dev` (no CSP) and in any browser, so three lanes in a row "verified" it. It
 * does not work in the shipped Tauri app: Tauri rewrites the configured CSP and
 * substitutes a per-load nonce for `__TAURI_STYLE_NONCE__` in `style-src`, and
 * under CSP Level 3 the presence of a nonce makes `'unsafe-inline'` be ignored.
 * A script-created `<style>` carries no nonce, so it was blocked — every
 * colour, font, radius, space and duration in the product silently vanished
 * while `styles.css` (a `'self'` stylesheet, so still allowed) kept its layout
 * rules. The result was a serif-on-white wireframe that still had a three
 * column grid.
 *
 * So the tokens are emitted here, at build time, as an ordinary CSS module that
 * Vite bundles into the same `'self'` stylesheet as everything else. No inline
 * style element, nothing for the CSP to refuse, and one fewer thing to happen
 * after first paint.
 */
const TOKENS_ID = "virtual:nesq-tokens.css"
const TOKENS_RESOLVED = `\0${TOKENS_ID}`

function hexToRgb(hex: string): [number, number, number] {
  const clean = hex.replace("#", "")
  const full =
    clean.length === 3
      ? clean
          .split("")
          .map((c) => c + c)
          .join("")
      : clean
  const int = Number.parseInt(full, 16)
  if (Number.isNaN(int)) return [0, 0, 0]
  return [(int >> 16) & 255, (int >> 8) & 255, int & 255]
}

function withAlpha(hex: string, alpha: number): string {
  const [r, g, b] = hexToRgb(hex)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

type PaletteLike = { [K in keyof Palette]: string }

/**
 * Washes, scrims and the two brand atmospherics.
 *
 * Every value is an alpha derivative of a role the design system already
 * audited — no new hue is invented, which is what keeps the 478-pair AA report
 * a true description of the app. Anything opaque enough to carry text comes
 * straight from the palette.
 */
function derivedVars(palette: PaletteLike, onAccent: string, glowAlpha: number): string {
  return [
    ["border-strong", withAlpha(palette.text, 0.24)],
    ["border-subtle", withAlpha(palette.text, 0.08)],
    ["accent-soft", withAlpha(palette.accent, 0.16)],
    ["accent-wash", withAlpha(palette.accent, 0.08)],
    ["on-accent", onAccent],
    ["danger-soft", withAlpha(palette.danger, 0.14)],
    ["success-soft", withAlpha(palette.success, 0.14)],
    ["warning-soft", withAlpha(palette.warning, 0.16)],
    ["scrim", withAlpha(palette.bg, 0.78)],
    ["skeleton", withAlpha(palette.text, 0.07)],
    ["skeleton-shine", withAlpha(palette.text, 0.14)],
    // One restrained bloom of the brand accent behind the app — not the
    // violet/fuchsia pair that used to sit here. The sweep is not the identity.
    ["glow", withAlpha(palette.accent, glowAlpha)],
    // The mark's own 60.3-degree diagonal, reused as texture. Deliberately
    // below the threshold of being noticed.
    ["hairline", withAlpha(palette.text, glowAlpha > 0.1 ? 0.012 : 0.014)],
  ]
    .map(([name, value]) => `  --${name}: ${value};`)
    .join("\n")
}

/**
 * The reduced-motion clamp, emitted once and emitted last.
 *
 * `cssVariablesFor` can attach its own `@media (prefers-reduced-motion: reduce)`
 * block to whichever selector it is given, and that is what used to happen for
 * the bare `:root` call. It did not work, and had never worked.
 *
 * `applyTheme` in `state/theme.tsx` always writes `data-theme` on `<html>`,
 * before first paint, on every boot. So `:root[data-theme="dark"]` (0,1,1) is
 * always in play, and it always beat a clamp sitting on bare `:root` (0,1,0).
 * A media query contributes no specificity of its own, so the preference lost
 * every time and `--duration-base` stayed at 200ms with reduced motion on.
 *
 * CSS mostly got away with it: `styles.css` carries a blanket
 * `animation-duration: 0.001ms !important` safety net for the reduce case.
 * JavaScript did not. `lib/motion.ts` reads these tokens to decide GSAP's
 * durations, and that is the entire mechanism by which the motion layer
 * honours the preference, so an unclamped token meant an unclamped animation.
 *
 * Hence: one block, naming all three selectors so it matches whatever the
 * theme attribute happens to say, emitted after them so that equal specificity
 * resolves in its favour.
 */
function reducedMotionBlock(): string {
  const selectors = [":root", ':root[data-theme="light"]', ':root[data-theme="dark"]'].join(",\n  ")
  const zeroed = Object.keys(durations)
    .map((key) => `    --duration-${key}: 0ms;`)
    .join("\n")
  return `\n@media (prefers-reduced-motion: reduce) {\n  ${selectors} {\n${zeroed}\n  }\n}\n`
}

/**
 * Both schemes, complete.
 *
 * `cssVariablesFor` emits the whole set under the selector it is given —
 * palette, radii, spacing, the type scale with its tracking and case,
 * elevation, motion, and every semantic role. The light block is emitted for
 * real rather than patched on top of the dark one, which is how the six risk
 * classes, twelve bot states and five status roles used to end up wearing
 * dark-derived colours in light mode. The reduced-motion block rides with the
 * dark set and is not repeated: it only zeroes `--duration-*`, which is
 * scheme-independent.
 */
export function buildTokenCss(): string {
  return [
    cssVariablesFor("dark", { selector: ":root", includeReducedMotion: false }),
    `:root {\n${derivedVars(darkPalette, brandNavy, 0.16)}\n  color-scheme: dark;\n}`,
    cssVariablesFor("light", { selector: ':root[data-theme="light"]', includeReducedMotion: false }),
    `:root[data-theme="light"] {\n${derivedVars(lightPalette, "#ffffff", 0.09)}\n  color-scheme: light;\n}`,
    cssVariablesFor("dark", { selector: ':root[data-theme="dark"]', includeReducedMotion: false }),
    `:root[data-theme="dark"] {\n${derivedVars(darkPalette, brandNavy, 0.16)}\n  color-scheme: dark;\n}`,
    // Last, deliberately. See reducedMotionBlock().
    reducedMotionBlock(),
  ].join("\n")
}

function nesqTokens(): Plugin {
  return {
    name: "nesq-design-tokens",
    resolveId(id) {
      return id === TOKENS_ID ? TOKENS_RESOLVED : null
    },
    load(id) {
      return id === TOKENS_RESOLVED ? buildTokenCss() : null
    },
  }
}

export default defineConfig({
  plugins: [nesqTokens(), react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    watch: {
      // Z: / Windows FS watchers can throw UNKNOWN and kill Vite
      usePolling: true,
      interval: 1000,
    },
  },
  resolve: {
    alias: {
      "@nesqbot/ui": path.resolve(__dirname, "../../packages/ui/src/index.ts"),
      "@nesqbot/protocol": path.resolve(__dirname, "../../packages/protocol/src/index.ts"),
    },
  },
})
