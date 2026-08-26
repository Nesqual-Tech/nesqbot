/**
 * The icon set.
 *
 * Replaces the emoji that used to stand in for navigation and empty-state
 * glyphs. Emoji are a different visual language on every OS, they carry their
 * own colour (so they ignore the palette entirely, including in light mode),
 * and at 14px they are mush. These are 24px-grid strokes on `currentColor`,
 * drawn with the same geometric, straight-line vocabulary as the mark.
 *
 * Deliberately not a dependency: nine paths is not worth 200 KB of icon
 * library, and a bundled set cannot inherit the token stroke weight.
 */
import type { SVGProps } from "react"

export type IconName =
  | "chat"
  | "shield"
  | "plug"
  | "repeat"
  | "chart"
  | "blocks"
  | "monitor"
  | "spark"
  | "sun"
  | "moon"
  | "refresh"
  | "alert"
  | "check"
  | "close"
  | "copy"
  | "plus"
  | "trash"
  | "user"
  | "bot"
  | "expand"
  | "collapse"
  | "keyboard"
  | "search"
  | "command"
  | "list"
  | "book"

/** Path data on a 24x24 grid. Stroked, never filled, so weight stays uniform. */
const PATHS: Record<IconName, string> = {
  chat: "M21 12a8 8 0 0 1-8 8H7l-4 3v-6.2A8 8 0 0 1 3 12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z",
  shield: "M12 3 5 6v6c0 4.2 2.8 7.6 7 9 4.2-1.4 7-4.8 7-9V6l-7-3ZM9 12l2.2 2.2L15.5 10",
  plug: "M9 3v5m6-5v5M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6V8Zm6 9v4",
  repeat: "M4 10V8a4 4 0 0 1 4-4h9m0 0-3-3m3 3-3 3M20 14v2a4 4 0 0 1-4 4H7m0 0 3 3m-3-3 3-3",
  chart: "M4 20V10m5 10V4m5 16v-7m5 7V8",
  blocks: "M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z",
  monitor: "M3 5h18v11H3V5Zm5 15h8m-4-4v4",
  spark: "M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2 2-6ZM19 4v3m1.5-1.5h-3",
  sun: "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm0-5v2m0 20v-2M2 12h2m18 0h-2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z",
  refresh: "M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5",
  alert: "M12 4 2.5 20h19L12 4Zm0 6v5m0 3h.01",
  check: "m4 12.5 5 5L20 6.5",
  close: "M6 6l12 12M18 6 6 18",
  copy: "M9 9h10v11H9V9ZM5 15V4h10",
  plus: "M12 5v14M5 12h14",
  trash: "M4 7h16M9 7V4h6v3m-9 0 1 13h10l1-13M10 11v5m4-5v5",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-8 8a8 8 0 0 1 16 0",
  bot: "M8 8h8a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-4a3 3 0 0 1 3-3Zm4 0V4m-2.5 8v1.5m5-1.5v1.5M2 12v3m20-3v3",
  // Corner brackets pushing out, and the same four pulling in. Same straight
  // vocabulary as the rest of the set, no arrowheads.
  expand: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  collapse: "M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5",
  keyboard: "M3 7h18v10H3V7Zm3.5 3h.01M10 10h.01M13.5 10h.01M17 10h.01M8 13.5h8",
  // The palette's own two glyphs. `command` is the loop-and-square that every
  // keyboard-first surface has trained people to read as "jump to anything";
  // it earns its place beside the search ring rather than duplicating it.
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 4 4",
  command:
    "M9 9h6v6H9V9Zm0 0V6a3 3 0 1 0-3 3h3Zm6 0V6a3 3 0 1 1 3 3h-3Zm-6 6v3a3 3 0 1 1-3-3h3Zm6 0v3a3 3 0 1 0 3-3h-3Z",
  // A ledger, not a to-do list: rows of a fixed width rather than checkboxes,
  // for the audit trail's "what happened, in order" rather than "what is
  // left to do".
  list: "M4 6h16M4 12h16M4 18h10",
  // Two pages meeting at a spine — the knowledge base, distinct from `list`'s
  // rows-of-equal-width ledger.
  book: "M12 6.5C10.5 5 8 4 4 4v14c4 0 6.5 1 8 2.5m0-14C13.5 5 16 4 20 4v14c-4 0-6.5 1-8 2.5m0-14v14",
}

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, "name"> {
  name: IconName
  /** Rendered size in px. The grid is 24, so anything scales cleanly. */
  size?: number
  /** Accessible name. Omit for a decorative icon (the default). */
  title?: string
}

export function Icon({ name, size = 18, title, className, ...rest }: IconProps) {
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      focusable="false"
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      <path d={PATHS[name]} />
    </svg>
  )
}
