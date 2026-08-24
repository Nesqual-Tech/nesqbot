/**
 * The Nesqual mark, rendered into the DOM.
 *
 * `@nesqbot/ui` ships the logo as SVG *source strings* rather than components,
 * because the same geometry has to serve this app (DOM) and the Expo app
 * (react-native-svg). Here that means one `dangerouslySetInnerHTML` — the
 * string is built by `logo.ts` from two hardcoded path constants and values
 * this file passes in, so there is no user data anywhere near it.
 *
 * Colour is theme-aware and never hardcoded: `ink` follows `--text` through
 * `currentColor`, and the accent comes from the audited palette (the sampled
 * `#8499da` on dark, `brandMark[600]` on light, which is the light palette's
 * `accent`). The white-on-transparent artwork would be invisible in light mode
 * otherwise.
 */
import { useMemo } from "react"
import { brand, getPalette, nesqualLockupSvg, nesqualMarkSvg } from "@nesqbot/ui"
import { useTheme } from "../state/theme"

export interface MarkProps {
  /** Height in px. Width follows the 142:171 aspect ratio. */
  size?: number
  /** Accessible name. Omitted means decorative (`aria-hidden`). */
  title?: string
  className?: string
}

/** The accent half of the mark, for the current scheme. */
function useMarkAccent(): string {
  const { theme } = useTheme()
  return getPalette(theme).accent
}

/** The bare "N" mark. Ink inherits `currentColor`. */
export function NesqualMark({ size = 24, title, className }: MarkProps) {
  const accent = useMarkAccent()
  const html = useMemo(
    () =>
      nesqualMarkSvg({
        size,
        ink: "currentColor",
        accent,
        title,
        className: "nesq-svg",
      }),
    [size, accent, title],
  )
  return <span className={className ? `mark ${className}` : "mark"} dangerouslySetInnerHTML={{ __html: html }} />
}

export interface LockupProps extends MarkProps {
  /** `"continuation"` is the real artwork: the mark *is* the N. */
  wordmark?: "continuation" | "full" | "none"
  tagline?: boolean
}

/** Mark + "ESQUAL", exactly as the artwork sets it. */
export function NesqualLockup({
  size = 22,
  wordmark = "continuation",
  tagline = false,
  title = brand.companyName,
  className,
}: LockupProps) {
  const accent = useMarkAccent()
  const html = useMemo(
    () =>
      nesqualLockupSvg({
        height: size,
        wordmark,
        tagline,
        ink: "currentColor",
        accent,
        title,
        className: "nesq-svg",
        // The builder emits `width` from `size`; passing `height` alone keeps
        // the intrinsic viewBox aspect and lets the row size itself.
        attributes: { preserveAspectRatio: "xMinYMid meet" },
      }),
    [size, wordmark, tagline, accent, title],
  )
  return <span className={className ? `mark ${className}` : "mark"} dangerouslySetInnerHTML={{ __html: html }} />
}

/**
 * The mark used as a quiet watermark behind an empty state. Deliberately not
 * an `<img>`: it has to pick up `currentColor` and the theme accent.
 */
export function NesqualWatermark({ size = 96 }: { size?: number }) {
  const accent = useMarkAccent()
  const html = useMemo(
    () => nesqualMarkSvg({ size, ink: "currentColor", accent, className: "nesq-svg" }),
    [size, accent],
  )
  return <span className="mark mark--watermark" aria-hidden="true" dangerouslySetInnerHTML={{ __html: html }} />
}
