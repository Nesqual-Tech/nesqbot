/**
 * Contrast and brand-integrity audit.
 *
 * The point of this module is that nothing in the design system claims WCAG
 * compliance in a comment. It claims it here, in a function anyone can run:
 *
 *   node packages/ui/scripts/check-brand.mjs
 *
 * It is deliberately data, not assertions — it returns rows and lets the caller
 * decide what to do with them, so the same audit backs the CLI check, a future
 * test and an in-app "accessibility" panel without being rewritten.
 */

import { BRAND_MARK_HEX, brand } from "./brand"
import { CONTRAST_THRESHOLDS, NON_TEXT_MIN, contrastRatioRounded } from "./color"
import { nesqualMarkPaths } from "./logo"
import { COLOR_SCHEMES, brandMark, getPalette, logoInk, type ColorScheme, type PaletteRole } from "./tokens"
import { BOT_STATE_TOKENS, RISK_CLASSES, botStateColors, riskColors, statusColors, type SemanticRole } from "./semantic"

/* ------------------------------------------------------------------ *
 * Contrast
 * ------------------------------------------------------------------ */

export type ContrastKind = "text" | "nonText"

export interface ContrastCheck {
  scheme: ColorScheme
  /** `palette`, `risk`, `botState`, `status`. */
  group: string
  /** The role being checked, e.g. `textMuted` or `delete.fg`. */
  name: string
  /** The colour under test. */
  fg: string
  /** What it is drawn on. */
  bg: string
  /** Which ground `bg` is, e.g. `surfaceRaised`, or `solid` for on-fill text. */
  ground: string
  kind: ContrastKind
  ratio: number
  required: number
  passes: boolean
}

const GROUND_ROLES: readonly PaletteRole[] = ["bg", "surface", "surfaceAlt", "surfaceRaised"]

/** Palette roles that carry body text and must clear AA on every ground. */
const TEXT_ROLES: readonly PaletteRole[] = ["text", "textMuted", "textDim"]

/**
 * Palette roles used both as fills and, routinely, as text — a red "Failed", an
 * accent-coloured link. Held to AA on the two grounds text actually sits on,
 * and to the non-text minimum on the two elevated ones.
 */
const ACCENT_ROLES: readonly PaletteRole[] = ["accent", "accentStrong", "danger", "success", "warning"]

function check(
  scheme: ColorScheme,
  group: string,
  name: string,
  fg: string,
  bg: string,
  ground: string,
  kind: ContrastKind,
  required: number,
): ContrastCheck {
  const ratio = contrastRatioRounded(fg, bg)
  return { scheme, group, name, fg, bg, ground, kind, ratio, required, passes: ratio >= required }
}

function auditRole(scheme: ColorScheme, group: string, name: string, role: SemanticRole): ContrastCheck[] {
  const palette = getPalette(scheme)
  const rows: ContrastCheck[] = []
  for (const ground of GROUND_ROLES) {
    rows.push(check(scheme, group, `${name}.fg`, role.fg, palette[ground], ground, "text", CONTRAST_THRESHOLDS.AA.body))
    rows.push(check(scheme, group, `${name}.solid`, role.solid, palette[ground], ground, "nonText", NON_TEXT_MIN))
  }
  rows.push(
    check(scheme, group, `${name}.onSolid`, role.onSolid, role.solid, "solid", "text", CONTRAST_THRESHOLDS.AA.body),
  )
  return rows
}

/** Every contrast pair the design system is responsible for, measured. */
export function contrastAudit(): ContrastCheck[] {
  const rows: ContrastCheck[] = []

  for (const scheme of COLOR_SCHEMES) {
    const palette = getPalette(scheme)

    for (const role of TEXT_ROLES) {
      for (const ground of GROUND_ROLES) {
        rows.push(
          check(scheme, "palette", role, palette[role], palette[ground], ground, "text", CONTRAST_THRESHOLDS.AA.body),
        )
      }
    }

    for (const role of ACCENT_ROLES) {
      for (const ground of GROUND_ROLES) {
        const primary = ground === "bg" || ground === "surface"
        rows.push(
          check(
            scheme,
            "palette",
            role,
            palette[role],
            palette[ground],
            ground,
            primary ? "text" : "nonText",
            primary ? CONTRAST_THRESHOLDS.AA.body : NON_TEXT_MIN,
          ),
        )
      }
    }

    for (const risk of RISK_CLASSES) rows.push(...auditRole(scheme, "risk", risk, riskColors[scheme][risk]))
    for (const state of BOT_STATE_TOKENS) {
      rows.push(...auditRole(scheme, "botState", state, botStateColors[scheme][state]))
    }
    for (const status of ["info", "success", "warning", "danger", "neutral"] as const) {
      rows.push(...auditRole(scheme, "status", status, statusColors[scheme][status]))
    }
  }

  return rows
}

export function contrastFailures(rows: readonly ContrastCheck[] = contrastAudit()): ContrastCheck[] {
  return rows.filter((row) => !row.passes)
}

/** Fixed-width table, for a terminal. */
export function formatContrastAudit(rows: readonly ContrastCheck[] = contrastAudit()): string {
  return rows
    .map(
      (r) =>
        `${r.passes ? "ok  " : "FAIL"} ${r.scheme.padEnd(5)} ${r.group.padEnd(8)} ${r.name.padEnd(26)} ` +
        `${r.fg} on ${r.bg} (${r.ground.padEnd(13)}) ${r.ratio.toFixed(2).padStart(5)}:1 ` +
        `needs ${r.required.toFixed(1)} [${r.kind}]`,
    )
    .join("\n")
}

/* ------------------------------------------------------------------ *
 * Brand integrity
 * ------------------------------------------------------------------ */

export interface BrandIssue {
  what: string
  detail: string
}

/**
 * Catch the failure mode that produced this whole exercise: the mark colour
 * drifting away from the artwork, in one place, silently.
 */
export function brandIntegrity(): BrandIssue[] {
  const issues: BrandIssue[] = []

  if (logoInk !== BRAND_MARK_HEX) {
    issues.push({ what: "logoInk", detail: `${logoInk} is not the sampled mark colour ${BRAND_MARK_HEX}` })
  }
  if (brandMark[400] !== BRAND_MARK_HEX) {
    issues.push({ what: "brandMark[400]", detail: `${brandMark[400]} is not the ramp anchor ${BRAND_MARK_HEX}` })
  }
  if (brand.markHex !== BRAND_MARK_HEX) {
    issues.push({ what: "brand.markHex", detail: `${brand.markHex} disagrees with BRAND_MARK_HEX` })
  }
  if (getPalette("dark").accent !== BRAND_MARK_HEX) {
    issues.push({ what: "darkPalette.accent", detail: "the native scheme's accent is not the mark colour" })
  }

  // The mark is nine straight lines and two closed subpaths. If either stops
  // being a closed polygon of straight segments, something has been pasted
  // over it from an export rather than edited.
  for (const [name, d] of Object.entries(nesqualMarkPaths)) {
    if (!/^M[\d\s.]+(L[\d\s.]+)+Z$/.test(d)) {
      issues.push({ what: `nesqualMarkPaths.${name}`, detail: "no longer a closed straight-line polygon" })
    }
  }

  return issues
}
