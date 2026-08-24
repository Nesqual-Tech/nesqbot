#!/usr/bin/env node
/**
 * Verify the design system against the things it claims about itself.
 *
 *   node packages/ui/scripts/check-brand.mjs
 *   node packages/ui/scripts/check-brand.mjs --verbose
 *
 * Three checks, in order of how badly they would bite:
 *
 *   1. Export surface. `@nesqbot/ui` is a source-only package imported by two
 *      apps; a rename here is a build break there, and nothing else in the repo
 *      would catch it. The baseline below is the full runtime export list as it
 *      stood before the brand pass. Additions are fine, removals are not.
 *   2. Brand integrity. The mark colour must be the sampled one, in every place
 *      that holds a copy of it. This exists because it had already drifted once
 *      (#8499d9 against the artwork's #8499da).
 *   3. Contrast. Every pair the palette is responsible for, measured against
 *      WCAG AA. See `src/audit.ts` for which rule applies where.
 *
 * Runs on Node's built-in TypeScript stripping (Node 22.18+ / 24), so there is
 * no build step and no dev dependency. The resolver hook exists only because
 * the package's own imports are extensionless for the bundlers.
 */

import { registerHooks } from "node:module"
import { existsSync } from "node:fs"
import { fileURLToPath, pathToFileURL } from "node:url"
import { dirname, resolve } from "node:path"

registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith(".") && !/\.[cm]?[jt]sx?$/.test(specifier) && context.parentURL) {
      const base = new URL(specifier, context.parentURL)
      for (const ext of [".ts", ".tsx", "/index.ts"]) {
        const candidate = new URL(base.href + ext)
        if (existsSync(fileURLToPath(candidate))) return next(candidate.href, context)
      }
    }
    return next(specifier, context)
  },
})

const verbose = process.argv.includes("--verbose")
const here = dirname(fileURLToPath(import.meta.url))
const ui = await import(pathToFileURL(resolve(here, "../src/index.ts")).href)

/**
 * The export surface before the brand pass. Every name here is imported by, or
 * reachable from, apps/desktop and apps/mobile.
 */
const LEGACY_EXPORTS = [
  "ATTENTION_BOT_STATES",
  "BOT_STATE_TOKENS",
  "COLOR_SCHEMES",
  "ELEVATION_LEVELS",
  "RISK_CLASSES",
  "accentSweep",
  "accentSweepCss",
  "botColors",
  "botDisplayName",
  "botInitials",
  "botStateColors",
  "botStateLabels",
  "brandGradient",
  "brandMark",
  "brandNavy",
  "companyName",
  "cssVariables",
  "cssVariablesFor",
  "darkCssVariables",
  "darkPalette",
  "durations",
  "easings",
  "elevation",
  "getBotColor",
  "getBotStateColor",
  "getMotion",
  "getPalette",
  "getRiskColor",
  "getShadow",
  "getStatusColor",
  "lightCssVariables",
  "lightPalette",
  "logoInk",
  "motion",
  "needsAttention",
  "productName",
  "radii",
  "radiusPill",
  "readableInk",
  "reducedMotion",
  "riskColors",
  "riskDescriptions",
  "riskLabels",
  "shadows",
  "spacing",
  "statusColors",
  "transition",
  "typeScale",
  "typography",
  "withAlpha",
]

let failed = false
const fail = (message) => {
  failed = true
  console.error(`FAIL  ${message}`)
}

/* 1 — export surface ------------------------------------------------ */

const present = new Set(Object.keys(ui))
const missing = LEGACY_EXPORTS.filter((name) => !present.has(name))
if (missing.length > 0) fail(`${missing.length} legacy export(s) no longer resolve: ${missing.join(", ")}`)
else console.log(`ok    ${LEGACY_EXPORTS.length} legacy exports all resolve (${present.size} exports total)`)

// cssVariables is a string that is also a function. Both halves are load-bearing.
if (typeof ui.cssVariables !== "function") fail("cssVariables is no longer callable")
else if (!`${ui.cssVariables}`.includes("--brand-mark:")) fail("cssVariables no longer coerces to the dark block")
else if (!ui.cssVariables("light").includes("--color-scheme: light"))
  fail("cssVariables('light') is not the light block")
else console.log("ok    cssVariables works as both a string and a function")

/* 2 — brand integrity ------------------------------------------------ */

const issues = ui.brandIntegrity()
if (issues.length > 0) for (const issue of issues) fail(`brand: ${issue.what} — ${issue.detail}`)
else console.log(`ok    brand integrity (mark ${ui.BRAND_MARK_HEX} consistent across tokens, ramp and palette)`)

// The SVG builders must produce something a parser will accept.
for (const [label, svg] of [
  ["mark", ui.nesqualMarkSvg({ size: 24 })],
  ["mono", ui.nesqualMarkMonoSvg("currentColor")],
  ["lockup", ui.nesqualLockupSvg({ tagline: true, title: "Nesqual Tech" })],
]) {
  if (!svg.startsWith("<svg ") || !svg.endsWith("</svg>")) fail(`${label} SVG is malformed`)
  else if ((svg.match(/</g) ?? []).length !== (svg.match(/>/g) ?? []).length) fail(`${label} SVG has unbalanced tags`)
}
if (!failed) console.log("ok    mark, monochrome and lockup SVGs are well formed")

/* 3 — contrast -------------------------------------------------------- */

const rows = ui.contrastAudit()
const failures = ui.contrastFailures(rows)
if (verbose) console.log(ui.formatContrastAudit(rows))
if (failures.length > 0) {
  fail(`${failures.length} of ${rows.length} contrast pairs are below their threshold:`)
  console.error(ui.formatContrastAudit(failures))
} else {
  console.log(`ok    all ${rows.length} contrast pairs meet their WCAG threshold`)
}

process.exit(failed ? 1 : 0)
