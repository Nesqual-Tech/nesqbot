/**
 * @nesqbot/ui — the Nesqual Tech design system for Nesq Bot.
 *
 * Layers, in dependency order:
 *
 *   brand.ts      durable facts — names, taglines, the sampled mark colour
 *   color.ts      colour maths — WCAG contrast, OKLCH, contrast repair
 *   tokens.ts     primitives — brand colours, palettes, scales, motion
 *   logo.ts       the mark, as SVG source (no JSX: both platforms consume it)
 *   semantic.ts   meaning — risk classes, bot states, status roles
 *   audit.ts      the measured contrast report behind the AA claims
 *   css.ts        emission — custom-property blocks for the web app
 *   components.ts strings and helpers shared by both apps
 *
 * There are no React components here on purpose: desktop renders DOM and
 * mobile renders React Native primitives, so the shared layer stops at values.
 *
 * Everything is exported from this barrel. Import from "@nesqbot/ui", never
 * from "@nesqbot/ui/src/tokens".
 */

export * from "./brand"
export * from "./color"
export * from "./tokens"
export * from "./logo"
export * from "./semantic"
export * from "./audit"
export * from "./css"
export * from "./components"
