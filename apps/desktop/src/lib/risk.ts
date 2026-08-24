/**
 * Risk, for the surfaces that are not the approval queue.
 *
 * ## Why this is not in `lib/approvals.ts`
 *
 * That module answers "what will approving *this row* do", and every function
 * in it takes an `Approval`. But the six risk classes are not a property of
 * approvals — they are a property of *capability*. A connector declares a risk
 * class per action at registration time, long before anything is held for a
 * decision, and that declaration is the most consequential fact about binding
 * it to a bot: `stripe` can commit money, `hubspot` can only read.
 *
 * The Integrations catalogue used to say `auth api_key · 2 actions` in muted
 * grey and put the risk classes two clicks away behind "Show actions", so the
 * one screen where a person *grants* a capability was the one screen that did
 * not mention risk. Everything here exists to fix that: the same vocabulary,
 * the same most-dangerous-first ordering and the same words the approval card
 * uses, over any list of things that carry a `risk`.
 */
import type { RiskClass } from "@nesqbot/protocol"

/**
 * Most dangerous first.
 *
 * The single ordering every risk list in the product sorts by, so a tally
 * strip, a catalogue and the approval queue all lead with the same class.
 */
export const RISK_ORDER: readonly RiskClass[] = ["delete", "spend", "send", "mutate", "draft", "observe"]

const RANK: Record<RiskClass, number> = {
  delete: 0,
  spend: 1,
  send: 2,
  mutate: 3,
  draft: 4,
  observe: 5,
}

/**
 * The three classes that always stop at a human.
 *
 * This is the product's governance rule, not a display preference — it is the
 * same split `packages/ui` colours warm and the orchestrator holds for
 * approval. Stated once, here, so no surface has to re-derive it from a
 * hardcoded list of three strings.
 */
export const GATED_RISKS: readonly RiskClass[] = ["send", "spend", "delete"]

export function isGated(risk: RiskClass | string): boolean {
  return (GATED_RISKS as readonly string[]).includes(risk)
}

/** Sort comparator: worst first, ties left alone. */
export function byRisk(a: RiskClass | string, b: RiskClass | string): number {
  return (RANK[a as RiskClass] ?? 99) - (RANK[b as RiskClass] ?? 99)
}

/**
 * The worst thing in a set of capabilities.
 *
 * A connector's identity on the catalogue card. `fallback` is the manifest's
 * `risk_default`, which is what a connector that declares no actions is still
 * permitted to do.
 */
export function highestRisk(items: ReadonlyArray<{ risk: RiskClass | string }>, fallback: RiskClass | string): RiskClass {
  let worst = RANK[fallback as RiskClass] === undefined ? "observe" : (fallback as RiskClass)
  for (const item of items) {
    if (byRisk(item.risk, worst) < 0) worst = item.risk as RiskClass
  }
  return worst
}

/** How many of each class, most dangerous first. Drives a tally strip. */
export function tallyRisks(
  items: ReadonlyArray<{ risk: RiskClass | string }>,
): Array<{ risk: RiskClass; count: number }> {
  const counts = new Map<RiskClass, number>()
  for (const item of items) {
    const risk = item.risk as RiskClass
    counts.set(risk, (counts.get(risk) ?? 0) + 1)
  }
  return RISK_ORDER.filter((risk) => counts.has(risk)).map((risk) => ({ risk, count: counts.get(risk) ?? 0 }))
}

/** How many of a set will stop for a human. */
export function gatedCount(items: ReadonlyArray<{ risk: RiskClass | string }>): number {
  return items.reduce((total, item) => total + (isGated(item.risk) ? 1 : 0), 0)
}
