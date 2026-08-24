/**
 * What an approval will actually do, in a sentence a person can act on.
 *
 * ## Why this exists
 *
 * The approval queue is the product's argument. Nesq Bot's pitch against Grok
 * Bot is not "it has an approvals screen" — everyone can build a list of
 * things with two buttons under them. It is *safety you can see*: that before
 * anything consequential happens, a human is told plainly what is about to
 * happen and whether it can be taken back.
 *
 * The card used to render `risk` as a 12px chip sitting between two other
 * chips of the same weight, and the payload as a key/value dump. Everything
 * needed to answer "should I press approve?" was on screen and none of it was
 * ranked, so a `delete` looked exactly like a `draft`.
 *
 * This module supplies the ranking: one consequence line, and one honest
 * statement about reversibility, derived from the risk class and the payload
 * the orchestrator already sends. No API change, no new field — the
 * information was always there, it was just never said out loud.
 */
import { parseApprovalPayload, type RiskClass } from "@nesqbot/protocol"
import type { Approval } from "../types"

/**
 * How recoverable the action is once it has run.
 *
 * Three levels, because two would be a lie. Most `send`s are not reversible in
 * any meaningful sense (an email is read the moment it lands) but they are not
 * destructive either; grouping them with `delete` would cry wolf, and grouping
 * them with `mutate` would understate them.
 */
export type Reversibility = "reversible" | "hard-to-undo" | "permanent"

export interface Consequence {
  /** Imperative summary of the effect. Shown at subheading weight. */
  line: string
  reversibility: Reversibility
  /** One clause explaining the reversibility rating. */
  undo: string
  /** True when the action leaves the company. Drives the "external" marker. */
  external: boolean
}

const REVERSIBILITY_BY_RISK: Record<RiskClass, Reversibility> = {
  observe: "reversible",
  draft: "reversible",
  mutate: "reversible",
  send: "hard-to-undo",
  spend: "hard-to-undo",
  delete: "permanent",
}

const UNDO_COPY: Record<Reversibility, string> = {
  reversible: "Can be undone from here.",
  "hard-to-undo": "Cannot be recalled once it has run.",
  permanent: "Permanent. There is no undo.",
}

/** Money mentioned anywhere in a payload's inputs, as a display string. */
function findAmount(input: Record<string, unknown>): string | null {
  for (const [key, value] of Object.entries(input)) {
    if (typeof value !== "number" || !Number.isFinite(value)) continue
    const k = key.toLowerCase()
    if (!/amount|total|cost|price|commit|charge|fee|budget/.test(k)) continue
    // The key usually names its own currency (`amount_eur`, `cost_usd`).
    const currency = /_(eur|usd|gbp)$/.exec(k)?.[1]?.toUpperCase()
    const formatted = value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    return currency ? `${formatted} ${currency}` : formatted
  }
  return null
}

/**
 * The consequence of approving.
 *
 * Reads the discriminated payload where there is one and falls back to the
 * risk class alone where there is not — rows predating the `kind` field still
 * have to say something true, and "this will change data outside this app" is
 * true of every `mutate` regardless of shape.
 */
export function consequenceOf(approval: Approval): Consequence {
  const risk = approval.risk as RiskClass
  const payload = parseApprovalPayload(approval.payload)
  const reversibility = REVERSIBILITY_BY_RISK[risk] ?? "hard-to-undo"
  const undo = UNDO_COPY[reversibility]

  if (payload?.kind === "message_only") {
    const to = payload.to?.trim()
    return {
      line: to ? `Sends this message to ${to}.` : "Sends this message outside the company.",
      reversibility,
      undo,
      external: true,
    }
  }

  if (payload?.kind === "connector_action") {
    const amount = findAmount(payload.input)
    const verb = risk === "delete" ? "Deletes through" : risk === "spend" ? "Commits money through" : "Runs"
    const money = amount ? ` Amount: ${amount}.` : ""
    return {
      line: `${verb} ${payload.connector_id} — ${payload.action}.${money}`,
      reversibility,
      undo,
      external: true,
    }
  }

  if (payload?.kind === "mcp_tool") {
    return {
      line: `Calls ${payload.tool} on the ${payload.mcp_id} server.`,
      reversibility,
      undo,
      external: false,
    }
  }

  if (payload?.kind === "desktop_steps") {
    /*
     * "Replays 1 recorded step on the bot's desktop" is true of every held
     * desktop action and tells nobody which one. Where the server wrote a plain
     * block, the consequence is the thing itself: *Clicks "Message" on
     * linkedin.com/in/andrei-pop.* Sentence-cased from `intent`, which is
     * imperative — the same string the chat reply and the card heading use.
     */
    if (payload.plain?.intent) {
      const { intent, place } = payload.plain
      const acts = `${intent.charAt(0).toUpperCase()}${intent.slice(1)}`.replace(/^(\w+)/, (verb) =>
        verb.endsWith("s") ? verb : `${verb}s`,
      )
      return {
        line: place ? `${acts} on ${place}.` : `${acts}.`,
        reversibility,
        undo,
        // A `send` from the bot's browser leaves the company exactly as an
        // emailed one does. Read off the risk class rather than assumed from
        // the surface it happens to go through.
        external: risk === "send",
      }
    }
    const n = payload.steps.length
    return {
      line: `Replays ${n} recorded ${n === 1 ? "step" : "steps"} on the bot's desktop.`,
      reversibility,
      undo,
      external: false,
    }
  }

  const fallback: Record<RiskClass, string> = {
    observe: "Reads data. Nothing changes.",
    draft: "Prepares something for review. Nothing leaves.",
    mutate: "Changes records this bot can reach.",
    send: "Sends something outside the company.",
    spend: "Commits money.",
    delete: "Destroys data.",
  }
  return { line: fallback[risk] ?? "Runs a held action.", reversibility, undo, external: risk === "send" }
}

/** Count of each risk class in a list. Drives the queue summary strip. */
export function riskTally(approvals: Approval[]): Array<{ risk: RiskClass; count: number }> {
  const counts = new Map<RiskClass, number>()
  for (const a of approvals) {
    const risk = a.risk as RiskClass
    counts.set(risk, (counts.get(risk) ?? 0) + 1)
  }
  // Most dangerous first: the queue's headline should be its worst item.
  const order: RiskClass[] = ["delete", "spend", "send", "mutate", "draft", "observe"]
  return order.filter((r) => counts.has(r)).map((risk) => ({ risk, count: counts.get(risk) ?? 0 }))
}
