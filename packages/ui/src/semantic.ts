/**
 * Semantic colour roles.
 *
 * `tokens.ts` says what colours exist. This file says what they *mean*: which
 * colour a `delete`-class action wears, what "awaiting approval" looks like.
 * Screens should reach for these, not for raw hexes — that is what keeps the
 * approval sheet on mobile and the approval row on desktop reading the same.
 */

import { adjustToContrast, readableOn, CONTRAST_THRESHOLDS } from "./color"
import { brandGradient, brandMark, brandNavy, getPalette, logoInk, type ColorScheme } from "./tokens"

/**
 * Every flat ground a role can land on, per scheme. Text roles are derived
 * against all four, so a risk chip is legible whether it sits on the page
 * background or on a raised card.
 */
function schemeGrounds(scheme: ColorScheme): readonly string[] {
  const p = getPalette(scheme)
  return [p.bg, p.surface, p.surfaceAlt, p.surfaceRaised]
}

export const contrastGrounds: Record<ColorScheme, readonly string[]> = {
  dark: schemeGrounds("dark"),
  light: schemeGrounds("light"),
}

/* ------------------------------------------------------------------ *
 * Role shape
 * ------------------------------------------------------------------ */

export interface SemanticRole {
  /** Full-strength colour. Chip fills, dots, progress bars. */
  solid: string
  /** Text and icons drawn *on the surface*, not on `solid`. */
  fg: string
  /** Tinted surface — a 12% wash of `solid`. */
  bg: string
  /** Hairline for the tinted surface — a 35% wash of `solid`. */
  border: string
  /** Text drawn on top of `solid`, chosen for contrast. */
  onSolid: string
}

/** Append an alpha channel to a `#rrggbb`. Understood by CSS and React Native. */
export function withAlpha(hex: string, alpha: number): string {
  const clamped = Math.max(0, Math.min(1, alpha))
  const byte = Math.round(clamped * 255)
    .toString(16)
    .padStart(2, "0")
  return `${hex}${byte}`
}

/**
 * Ink that stays legible on `background`.
 *
 * This used to threshold Rec.601 perceived lightness at 0.6, which picked the
 * *worse* of the two inks for seven of the colours in this file — white on
 * #8a92ad measured 3.09:1 where navy measured 6.25:1. It now measures both and
 * takes the winner.
 */
export function readableInk(background: string): string {
  return readableOn(background, ["#ffffff", brandNavy])
}

/**
 * Build a role from its solid.
 *
 * `solid` is the designed colour and is used verbatim — it is a fill, a dot or
 * a progress bar, and it only has to clear 3:1 as a non-text element.
 *
 * `fg` is the same colour *as text*, so it is lifted (dark) or deepened
 * (light) in OKLCH until it clears 4.5:1 against every ground in the scheme.
 * Hue and chroma are held, so a red chip label is still the same red — just a
 * legible one. Before this, `fg` was simply `solid`, and eleven of the
 * thirty-six roles here failed AA as text.
 */
function role(solid: string, scheme: ColorScheme): SemanticRole {
  return {
    solid,
    fg: adjustToContrast(solid, contrastGrounds[scheme], CONTRAST_THRESHOLDS.AA.body),
    bg: withAlpha(solid, 0.12),
    border: withAlpha(solid, 0.35),
    onSolid: readableInk(solid),
  }
}

/* ------------------------------------------------------------------ *
 * Risk classes
 * ------------------------------------------------------------------ */

/**
 * Mirrors `RiskClass` in `@nesqbot/protocol`. Duplicated rather than imported
 * because this package is consumed by Metro with only `@nesqbot/ui` aliased,
 * and a design-token package should not drag the API contract in behind it.
 * If you change one, change the other.
 */
export type RiskClass = "observe" | "draft" | "mutate" | "send" | "spend" | "delete"

export const RISK_CLASSES = [
  "observe",
  "draft",
  "mutate",
  "send",
  "spend",
  "delete",
] as const satisfies readonly RiskClass[]

/**
 * Cool for the three classes a bot may run on its own, warm for the three
 * that always stop at a human. That split is the whole governance story in
 * one glance, so keep it: nothing safe should ever be warm.
 */
const RISK_SOLIDS: Record<ColorScheme, Record<RiskClass, string>> = {
  dark: {
    observe: "#7c8aa8",
    // brandGradient.indigo (#6366f1) verbatim put white text on the chip at
    // 4.47:1 — a hair under AA, and navy is worse. Two steps darker in OKLCH
    // fixes it (4.78:1) and still reads as the same indigo.
    draft: "#5f61eb",
    mutate: "#38bdf8",
    send: "#f59e0b",
    spend: "#f97316",
    delete: "#ef4444",
  },
  light: {
    observe: "#5a6280",
    draft: "#4f46e5",
    mutate: "#0284c7",
    send: "#b45309",
    spend: "#c2410c",
    delete: "#dc2626",
  },
}

export const riskColors: Record<ColorScheme, Record<RiskClass, SemanticRole>> = {
  dark: {
    observe: role(RISK_SOLIDS.dark.observe, "dark"),
    draft: role(RISK_SOLIDS.dark.draft, "dark"),
    mutate: role(RISK_SOLIDS.dark.mutate, "dark"),
    send: role(RISK_SOLIDS.dark.send, "dark"),
    spend: role(RISK_SOLIDS.dark.spend, "dark"),
    delete: role(RISK_SOLIDS.dark.delete, "dark"),
  },
  light: {
    observe: role(RISK_SOLIDS.light.observe, "light"),
    draft: role(RISK_SOLIDS.light.draft, "light"),
    mutate: role(RISK_SOLIDS.light.mutate, "light"),
    send: role(RISK_SOLIDS.light.send, "light"),
    spend: role(RISK_SOLIDS.light.spend, "light"),
    delete: role(RISK_SOLIDS.light.delete, "light"),
  },
}

export function getRiskColor(risk: RiskClass, scheme: ColorScheme = "dark"): SemanticRole {
  return riskColors[scheme][risk] ?? riskColors[scheme].observe
}

/** Short label for a risk chip. */
export const riskLabels: Record<RiskClass, string> = {
  observe: "Read only",
  draft: "Draft",
  mutate: "Change",
  send: "Send",
  spend: "Spend",
  delete: "Delete",
}

/** One line explaining what the class permits. Use as chip tooltip / sheet copy. */
export const riskDescriptions: Record<RiskClass, string> = {
  observe: "Reads data. Nothing changes.",
  draft: "Prepares something for review. Nothing leaves.",
  mutate: "Changes internal records.",
  send: "Sends something outside the company. Needs approval.",
  spend: "Commits money. Needs approval.",
  delete: "Destroys data. Needs approval.",
}

/* ------------------------------------------------------------------ *
 * Bot state
 * ------------------------------------------------------------------ */

/**
 * Two overlapping vocabularies live here on purpose: the Bot Desktop
 * lifecycle (`absent` … `error`, mirroring `BotDesktopState`) and what the bot
 * is doing in a thread (`idle` … `blocked`). A bot list cell shows one dot and
 * needs a colour for whichever it happens to be reporting.
 */
export type BotStateToken =
  | "absent"
  | "starting"
  | "running"
  | "suspended"
  | "stopping"
  | "error"
  | "idle"
  | "thinking"
  | "acting"
  | "awaiting_approval"
  | "blocked"
  | "offline"

export const BOT_STATE_TOKENS = [
  "absent",
  "starting",
  "running",
  "suspended",
  "stopping",
  "error",
  "idle",
  "thinking",
  "acting",
  "awaiting_approval",
  "blocked",
  "offline",
] as const satisfies readonly BotStateToken[]

/**
 * Dot and fill colours. These are non-text (WCAG 1.4.11), so the bar is 3:1
 * against the scheme's grounds, not 4.5:1 — `role()` derives the text variant
 * separately. Two entries had to move to clear even that: dark
 * `absent`/`offline` were #4b5372 (2.38:1 on `surface`, 1.75:1 on
 * `surfaceRaised` — an invisible dot), and light `absent`/`idle`/`offline` were
 * #8a92ad (2.72:1 on `surfaceAlt`).
 */
const BOT_STATE_SOLIDS: Record<ColorScheme, Record<BotStateToken, string>> = {
  dark: {
    absent: "#687191",
    starting: "#38bdf8",
    running: "#22c55e",
    suspended: "#f59e0b",
    stopping: "#94a3b8",
    error: "#ef4444",
    idle: "#6b7394",
    thinking: logoInk,
    acting: brandGradient.cyan,
    awaiting_approval: "#f59e0b",
    blocked: "#f97316",
    offline: "#687191",
  },
  light: {
    absent: "#7c849f",
    starting: "#0284c7",
    running: "#008338", // was #16a34a — 2.90:1 on `surfaceAlt`, invisible as a dot
    suspended: "#b45309",
    stopping: "#64748b",
    error: "#dc2626",
    idle: "#7c849f",
    thinking: brandMark[600],
    acting: "#0891b2",
    awaiting_approval: "#b45309",
    blocked: "#c2410c",
    offline: "#7c849f",
  },
}

export const botStateColors: Record<ColorScheme, Record<BotStateToken, SemanticRole>> = {
  dark: buildStateRoles("dark"),
  light: buildStateRoles("light"),
}

function buildStateRoles(scheme: ColorScheme): Record<BotStateToken, SemanticRole> {
  const source = BOT_STATE_SOLIDS[scheme]
  const out = {} as Record<BotStateToken, SemanticRole>
  for (const token of BOT_STATE_TOKENS) {
    out[token] = role(source[token], scheme)
  }
  return out
}

export function getBotStateColor(state: string, scheme: ColorScheme = "dark"): SemanticRole {
  const table = botStateColors[scheme]
  return table[state as BotStateToken] ?? table.idle
}

export const botStateLabels: Record<BotStateToken, string> = {
  absent: "No desktop",
  starting: "Starting",
  running: "Running",
  suspended: "Suspended",
  stopping: "Stopping",
  error: "Error",
  idle: "Idle",
  thinking: "Thinking",
  acting: "Working",
  awaiting_approval: "Waiting on you",
  blocked: "Blocked",
  offline: "Offline",
}

/** States that need the user to do something before the bot can continue. */
export const ATTENTION_BOT_STATES = ["awaiting_approval", "blocked", "error"] as const

export function needsAttention(state: string): boolean {
  return (ATTENTION_BOT_STATES as readonly string[]).includes(state)
}

/* ------------------------------------------------------------------ *
 * Status roles (generic feedback)
 * ------------------------------------------------------------------ */

export type StatusRole = "info" | "success" | "warning" | "danger" | "neutral"

export const statusColors: Record<ColorScheme, Record<StatusRole, SemanticRole>> = {
  dark: {
    info: role("#38bdf8", "dark"),
    success: role("#22c55e", "dark"),
    warning: role("#f59e0b", "dark"),
    danger: role("#ef4444", "dark"),
    neutral: role("#6b7394", "dark"),
  },
  light: {
    info: role("#0284c7", "light"),
    success: role("#008338", "light"), // was #16a34a — see the note on the bot-state table
    warning: role("#b45309", "light"),
    danger: role("#dc2626", "light"),
    neutral: role("#5a6280", "light"),
  },
}

export function getStatusColor(status: StatusRole, scheme: ColorScheme = "dark"): SemanticRole {
  return statusColors[scheme][status]
}
