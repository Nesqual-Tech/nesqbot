/** Shared presentational helpers (framework-agnostic strings / class hints). */

import { brand } from "./brand"
import { accentSweep } from "./tokens"

/**
 * Kept here because two apps already import them from this module. The values
 * come from `brand.ts` — that is the single source of truth, and these are
 * aliases, not a second copy.
 */
export const productName = brand.productName
export const companyName = brand.companyName

/** Display name for a bot slug. Unknown slugs are title-cased. */
export function botDisplayName(slug: string): string {
  const map: Record<string, string> = {
    chief_of_staff: "Chief of Staff",
    lead_generator: "Lead Generator",
    sales: "Sales",
    ops: "Ops",
    support: "Support",
  }
  return map[slug] ?? slug.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
}

/** One or two letters for an avatar chip. */
export function botInitials(slug: string): string {
  const words = botDisplayName(slug).split(/\s+/).filter(Boolean)
  const first = words[0]?.[0] ?? "?"
  const second = words.length > 1 ? (words[words.length - 1]?.[0] ?? "") : ""
  return (first + second).toUpperCase()
}

/**
 * The Nesqual accent sweep as a CSS gradient. Derived from `accentSweep`.
 *
 * Worth saying plainly: this is not the brand. The real identity is one accent
 * on dark. Use the sweep for at most one element per screen — a hero, a splash,
 * a progress bar — and reach for `brandMark` everywhere else. An accent that
 * appears on every card is not an accent.
 */
export function accentSweepCss(angle = "90deg"): string {
  return `linear-gradient(${angle}, ${accentSweep[0]}, ${accentSweep[1]} 50%, ${accentSweep[2]})`
}
