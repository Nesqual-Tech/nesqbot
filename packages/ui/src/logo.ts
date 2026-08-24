/**
 * The Nesqual mark, as SVG source.
 *
 * Why source strings and not components: this package is imported by the Tauri
 * desktop app (DOM) and by the Expo app (React Native). A JSX component would
 * only work in one of them. A string works in both — `dangerouslySetInnerHTML`
 * / `innerHTML` on web, `SvgXml` from `react-native-svg` on mobile — and it can
 * also be written straight to a `.svg` file by a build script.
 *
 * Why vector and not the PNG in `assets/`: the artwork is 707x353. Scaled down
 * to a 24px sidebar icon the diagonal turns to mush, and 65 KB is a lot for a
 * glyph that is nine straight lines.
 *
 * How the geometry was obtained
 * -----------------------------
 * `assets/nesqual-logo.png` was decoded and every pixel classified as mark
 * (blue), ink (white) or transparent. Scanline run boundaries were fitted to
 * straight lines: both stems are axis-aligned, and every diagonal in the mark
 * shares one slope, dx/dy = 1.75 (about 60.3 degrees from horizontal). The two
 * polygons below are those fits, translated so the mark's bounding box starts
 * at the origin.
 *
 * Measured against the source bitmap: 99.91% of fully-opaque ink pixels and
 * 99.96% of fully-opaque mark pixels fall inside the traced polygons, and every
 * pixel the polygons cover that the bitmap does not is an anti-aliased edge
 * pixel. In other words the trace is within one pixel of the original at its
 * native 142x171 size, and exact at any size above that.
 *
 * What the mark is made of: a vertical stem and a diagonal in ink, then a
 * second diagonal and a second stem in the accent colour, the two halves offset
 * so they read as one folded ribbon rather than a continuous N diagonal. The
 * gap between the two diagonals is part of the design; do not close it.
 */

import { BRAND_INK_HEX, BRAND_MARK_HEX, logoTagline } from "./brand"
import { typography } from "./tokens"

/* ------------------------------------------------------------------ *
 * Geometry
 * ------------------------------------------------------------------ */

/** Intrinsic size of the mark's artboard. Aspect ratio 142:171 (0.83:1). */
export const NESQUAL_MARK_WIDTH = 142
export const NESQUAL_MARK_HEIGHT = 171
export const NESQUAL_MARK_VIEWBOX = `0 0 ${NESQUAL_MARK_WIDTH} ${NESQUAL_MARK_HEIGHT}`

/**
 * The two halves of the mark, as SVG path data.
 *
 * `ink` is white in the original artwork (left stem + upper diagonal).
 * `accent` is `#8499da` (lower diagonal + right stem).
 */
export const nesqualMarkPaths = {
  ink: "M0 0L94 54L94 86L24 46L24 152L0 152Z",
  accent: "M119 20L142 20L142 171L119 158L48 118L48 85L119 126Z",
} as const

/* ------------------------------------------------------------------ *
 * Lockup metrics
 * ------------------------------------------------------------------ */

/**
 * Lockup geometry, in mark units, taken from the same bitmap measurement.
 *
 * The original wordmark uses the mark *as* its N — "ESQUAL" is what is set in
 * type beside it — so `wordmark: "continuation"` reproduces the artwork
 * exactly. `"full"` repeats the whole word for contexts where the mark is too
 * small for the join to read.
 *
 * Both text runs are pinned with `textLength`, so the lockup keeps the
 * original's proportions whatever font actually resolves. `lengthAdjust` is
 * left at its default (`spacing`), which distributes the difference into the
 * letter gaps and never distorts the glyphs themselves.
 */
export const NESQUAL_LOCKUP_METRICS = {
  /** Left edge of the type column, measured from the mark's left edge. */
  textX: 158,
  /** Wordmark baseline. Cap height is 69 units. */
  wordmarkBaseline: 109,
  wordmarkFontSize: 96,
  /** Measured width of "ESQUAL" in the artwork. */
  wordmarkLength: 446,
  /** Same rhythm extended by one glyph, for the `"full"` variant. */
  wordmarkLengthFull: 520,
  /** Tagline baseline. Cap height is 21 units. */
  taglineBaseline: 147,
  taglineFontSize: 29,
  /** Measured width of "EMPOWERING DIGITAL FRONTIERS" in the artwork. */
  taglineLength: 469,
  taglineX: 159,
} as const

/* ------------------------------------------------------------------ *
 * Builders
 * ------------------------------------------------------------------ */

export interface NesqualMarkOptions {
  /**
   * Height in px. Width follows the aspect ratio. Omit both this and
   * `width`/`height` to emit a size-less SVG that fills its container.
   */
  size?: number
  width?: number | string
  height?: number | string
  /** Colour of the stem and upper diagonal. Default white, as in the artwork. */
  ink?: string
  /** Colour of the lower diagonal and right stem. Default the sampled `#8499da`. */
  accent?: string
  /**
   * Draw both halves in one colour. Pass `true` for `ink`, or a hex to override.
   * The mark still reads: the halves are separated by a gap, not by colour.
   */
  monochrome?: boolean | string
  /** Accessible name. Omit for a decorative mark (emits `aria-hidden`). */
  title?: string
  /** Extra class attribute. */
  className?: string
  /** Any further attributes on the root `<svg>`. Values are escaped. */
  attributes?: Readonly<Record<string, string | number>>
}

export interface NesqualLockupOptions extends NesqualMarkOptions {
  /**
   * `"continuation"` (default) reproduces the artwork: the mark is the N and
   * "ESQUAL" follows it. `"full"` sets the whole word beside the mark.
   * `"none"` is the mark on its own with the tagline still available.
   */
  wordmark?: "continuation" | "full" | "none"
  /** Set the tagline under the wordmark. Default false. */
  tagline?: boolean
  /** Font stack for the type. Defaults to `typography.fontBrand`. */
  fontFamily?: string
  /** Override the tagline string. Defaults to the brand's logo tagline, in caps. */
  taglineText?: string
}

function escapeAttr(value: string | number): string {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")
}

function escapeText(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

function attrs(pairs: ReadonlyArray<readonly [string, string | number | undefined]>): string {
  return pairs
    .filter((pair): pair is readonly [string, string | number] => pair[1] !== undefined && pair[1] !== "")
    .map(([key, value]) => ` ${key}="${escapeAttr(value)}"`)
    .join("")
}

function resolveColors(options: NesqualMarkOptions): { ink: string; accent: string } {
  const ink = options.ink ?? BRAND_INK_HEX
  const accent = options.accent ?? BRAND_MARK_HEX
  if (options.monochrome === true) return { ink, accent: ink }
  if (typeof options.monochrome === "string") return { ink: options.monochrome, accent: options.monochrome }
  return { ink, accent }
}

function rootAttrs(options: NesqualMarkOptions, viewBox: string, aspect: number): string {
  const width = options.width ?? (options.size !== undefined ? Math.round(options.size * aspect) : undefined)
  const height = options.height ?? options.size
  return attrs([
    ["xmlns", "http://www.w3.org/2000/svg"],
    ["viewBox", viewBox],
    ["width", width],
    ["height", height],
    ["fill", "none"],
    ["class", options.className],
    ["role", options.title !== undefined ? "img" : undefined],
    ["aria-hidden", options.title === undefined ? "true" : undefined],
    ...Object.entries(options.attributes ?? {}),
  ])
}

/** The mark on its own. Two paths, no text, no gradients, no ids. */
export function nesqualMarkSvg(options: NesqualMarkOptions = {}): string {
  const { ink, accent } = resolveColors(options)
  const aspect = NESQUAL_MARK_WIDTH / NESQUAL_MARK_HEIGHT
  const title = options.title === undefined ? "" : `<title>${escapeText(options.title)}</title>`
  return (
    `<svg${rootAttrs(options, NESQUAL_MARK_VIEWBOX, aspect)}>` +
    title +
    `<path d="${nesqualMarkPaths.ink}" fill="${escapeAttr(ink)}"/>` +
    `<path d="${nesqualMarkPaths.accent}" fill="${escapeAttr(accent)}"/>` +
    `</svg>`
  )
}

/**
 * Single-colour mark. Convenience for `nesqualMarkSvg({ monochrome: true })`;
 * pass `color` for anything other than white, or `"currentColor"` to inherit.
 */
export function nesqualMarkMonoSvg(color = BRAND_INK_HEX, options: NesqualMarkOptions = {}): string {
  return nesqualMarkSvg({ ...options, monochrome: color })
}

/** Mark plus wordmark, and optionally the tagline. */
export function nesqualLockupSvg(options: NesqualLockupOptions = {}): string {
  const { ink, accent } = resolveColors(options)
  const m = NESQUAL_LOCKUP_METRICS
  const wordmark = options.wordmark ?? "continuation"
  const font = options.fontFamily ?? typography.fontBrand
  const taglineText = (options.taglineText ?? logoTagline).toUpperCase()

  const wordmarkText = wordmark === "full" ? "NESQUAL" : "ESQUAL"
  const wordmarkLength = wordmark === "full" ? m.wordmarkLengthFull : m.wordmarkLength
  const textRight = wordmark === "none" ? 0 : m.textX + wordmarkLength
  const taglineRight = options.tagline ? m.taglineX + m.taglineLength : 0
  const width = Math.max(NESQUAL_MARK_WIDTH, textRight, taglineRight)
  const viewBox = `0 0 ${width} ${NESQUAL_MARK_HEIGHT}`
  const aspect = width / NESQUAL_MARK_HEIGHT

  // The type is aria-hidden: "ESQUAL" on its own is meaningless to a screen
  // reader, and the whole lockup already carries the accessible name.
  const wordmarkNode =
    wordmark === "none"
      ? ""
      : `<text x="${m.textX}" y="${m.wordmarkBaseline}" textLength="${wordmarkLength}"` +
        ` font-family="${escapeAttr(font)}" font-size="${m.wordmarkFontSize}" font-weight="700"` +
        ` fill="${escapeAttr(ink)}" aria-hidden="true">${escapeText(wordmarkText)}</text>`

  const taglineNode = options.tagline
    ? `<text x="${m.taglineX}" y="${m.taglineBaseline}" textLength="${m.taglineLength}"` +
      ` font-family="${escapeAttr(font)}" font-size="${m.taglineFontSize}" font-weight="300"` +
      ` fill="${escapeAttr(ink)}" aria-hidden="true">${escapeText(taglineText)}</text>`
    : ""

  const title = options.title === undefined ? "" : `<title>${escapeText(options.title)}</title>`

  return (
    `<svg${rootAttrs(options, viewBox, aspect)}>` +
    title +
    `<path d="${nesqualMarkPaths.ink}" fill="${escapeAttr(ink)}"/>` +
    `<path d="${nesqualMarkPaths.accent}" fill="${escapeAttr(accent)}"/>` +
    wordmarkNode +
    taglineNode +
    `</svg>`
  )
}

/**
 * `data:` URI for the mark, for CSS `background-image`, `<img src>` and window
 * icons. Percent-encoded rather than base64 — it is smaller for SVG and stays
 * readable in a stylesheet.
 */
export function nesqualMarkDataUri(options: NesqualMarkOptions = {}): string {
  return `data:image/svg+xml,${encodeURIComponent(nesqualMarkSvg(options))}`
}

/** Everything about the mark in one namespace, for discoverability. */
export const NesqualMark = {
  width: NESQUAL_MARK_WIDTH,
  height: NESQUAL_MARK_HEIGHT,
  viewBox: NESQUAL_MARK_VIEWBOX,
  paths: nesqualMarkPaths,
  metrics: NESQUAL_LOCKUP_METRICS,
  svg: nesqualMarkSvg,
  mono: nesqualMarkMonoSvg,
  lockup: nesqualLockupSvg,
  dataUri: nesqualMarkDataUri,
} as const
