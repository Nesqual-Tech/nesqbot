/**
 * @nesqbot/model-router — cost prediction for the TypeScript clients.
 *
 * SOURCE OF TRUTH: `apps/api/app/services/model_router.py`.
 *
 * The Python router owns real routing: it holds the Azure credentials, calls
 * the deployments, records the cost ledger and enforces budgets. This package
 * is deliberately the *pure* half of it — the tier table, the routing rules
 * and the arithmetic — so the UI can answer "what will this cost?" and "which
 * model will answer me?" without a round trip, and so a budget meter can
 * update while a stream is still open.
 *
 * `TIER_PRICES`, `routeTask()` and `estimateCostUsd()` mirror `TIER_PRICES`,
 * `route_task()` and `estimate_cost_usd()` line for line. If you change a
 * number here, change it there in the same commit — a client that predicts a
 * different price than the server charges is worse than no prediction.
 */

import type { ModelTier } from "@nesqbot/protocol"

export type { ModelTier }

export const MODEL_TIERS = ["nano", "mini", "reason", "embed"] as const satisfies readonly ModelTier[]

export interface TierPrice {
  /** USD per 1M input tokens. */
  input: number
  /** USD per 1M output tokens. */
  output: number
  /** Env var holding the Azure deployment name for this tier. */
  deploymentEnv: string
}

/**
 * USD per 1M tokens — the same numbers the server bills from.
 *
 * Mirrors `TIER_PRICES` in `apps/api/app/services/model_router.py`, which
 * stores them as `(input, output)` tuples. Source: Azure AI Foundry list
 * pricing for the `swedencentral` deployments below, last checked 2026-08-22.
 */
export const TIER_PRICES: Record<ModelTier, TierPrice> = {
  nano: { input: 0.2, output: 1.2, deploymentEnv: "AZURE_DEPLOYMENT_NANO" },
  mini: { input: 0.75, output: 4.5, deploymentEnv: "AZURE_DEPLOYMENT_MINI" },
  reason: { input: 0.2, output: 0.5, deploymentEnv: "AZURE_DEPLOYMENT_REASON" },
  embed: { input: 0.02, output: 0, deploymentEnv: "AZURE_DEPLOYMENT_EMBED" },
}

/** Default Azure AI Foundry deployment behind each tier (see `.env.example`). */
export const TIER_DEFAULT_DEPLOYMENTS: Record<ModelTier, string> = {
  nano: "gpt-5.6-luna",
  mini: "gpt-5.4-mini",
  reason: "gpt-5.6-sol",
  embed: "text-embedding-3-small",
}

/** What each tier is for, in one line. Safe to surface in the UI. */
export const TIER_DESCRIPTIONS: Record<ModelTier, string> = {
  nano: "Cheapest. Classification, routing and context compaction.",
  mini: "Workhorse. Every normal agent turn.",
  reason: "Escalation only. Deep planning and repeated desktop failures.",
  embed: "Embeddings for memories and the knowledge base.",
}

export type TaskClass = "classify" | "route" | "agent_turn" | "computer_use_recover" | "deep_plan" | "compact" | "embed"

export const TASK_CLASSES = [
  "classify",
  "route",
  "agent_turn",
  "computer_use_recover",
  "deep_plan",
  "compact",
  "embed",
] as const satisfies readonly TaskClass[]

/**
 * Pick the tier for a task.
 *
 * Mirrors `route_task(task, fail_count)`. The only dynamic rule is desktop
 * recovery: the first two attempts stay on `mini`, and only the third attempt —
 * after two recorded failures — is worth escalating to the reasoning model.
 */
export function routeTask(task: TaskClass, opts?: { failCount?: number }): ModelTier {
  switch (task) {
    case "classify":
    case "route":
    case "compact":
      return "nano"
    case "embed":
      return "embed"
    case "computer_use_recover":
      return (opts?.failCount ?? 0) >= 2 ? "reason" : "mini"
    case "deep_plan":
      return "reason"
    case "agent_turn":
    default:
      return "mini"
  }
}

/** Mirrors `estimate_cost_usd(tier, input_tokens, output_tokens)`. */
export function estimateCostUsd(tier: ModelTier, inputTokens: number, outputTokens: number): number {
  const p = TIER_PRICES[tier]
  return (inputTokens / 1_000_000) * p.input + (outputTokens / 1_000_000) * p.output
}

/**
 * Rough token count for a piece of text.
 *
 * Four characters per token, the same heuristic the Python mock path uses. It
 * is wrong by 10–20% on real prose — good enough for a pre-flight estimate,
 * never good enough to bill from. Actual usage always comes back on the
 * `done` SSE event.
 */
export function estimateTokens(text: string): number {
  return Math.max(1, Math.ceil(text.length / 4))
}

export interface TurnCostEstimate {
  tier: ModelTier
  inputTokens: number
  outputTokens: number
  costUsd: number
}

/**
 * Predict the cost of a turn before sending it.
 *
 * Give it the prompt (or its token count) and an expected reply length; it
 * routes the task and prices the result. Use it to show "~$0.0004" next to
 * the send button, and to warn before a `deep_plan` escalation.
 */
export function estimateTurnCost(input: {
  task: TaskClass
  prompt?: string
  inputTokens?: number
  expectedOutputTokens?: number
  failCount?: number
}): TurnCostEstimate {
  const tier = routeTask(input.task, { failCount: input.failCount })
  const inputTokens = input.inputTokens ?? (input.prompt !== undefined ? estimateTokens(input.prompt) : 0)
  const outputTokens = input.expectedOutputTokens ?? (tier === "embed" ? 0 : 400)
  return {
    tier,
    inputTokens,
    outputTokens,
    costUsd: estimateCostUsd(tier, inputTokens, outputTokens),
  }
}

/* ------------------------------------------------------------------ *
 * Budget
 * ------------------------------------------------------------------ */

/**
 * Soft stop. Mirrors the orchestrator's check: at or over the daily budget the
 * bot answers with a budget notice instead of calling a model. It is a soft
 * cap — in-flight work finishes, nothing is killed mid-turn.
 */
export function shouldSoftStop(spentUsd: number, budgetUsd: number): boolean {
  return spentUsd >= budgetUsd
}

export type BudgetState = "ok" | "warning" | "critical" | "exhausted"

export interface BudgetStatus {
  state: BudgetState
  spentUsd: number
  budgetUsd: number
  remainingUsd: number
  /** 0–1, clamped. Drive a meter with this. */
  fraction: number
  blocked: boolean
}

/** Classify daily spend for a budget meter. Warns at 75%, critical at 90%. */
export function budgetStatus(spentUsd: number, budgetUsd: number): BudgetStatus {
  const safeBudget = budgetUsd > 0 ? budgetUsd : 0
  const fraction = safeBudget === 0 ? 1 : Math.min(1, Math.max(0, spentUsd / safeBudget))
  const blocked = shouldSoftStop(spentUsd, safeBudget)
  const state: BudgetState = blocked ? "exhausted" : fraction >= 0.9 ? "critical" : fraction >= 0.75 ? "warning" : "ok"
  return {
    state,
    spentUsd,
    budgetUsd: safeBudget,
    remainingUsd: Math.max(0, safeBudget - spentUsd),
    fraction,
    blocked,
  }
}

/* ------------------------------------------------------------------ *
 * Formatting
 * ------------------------------------------------------------------ */

/**
 * Format a USD amount for display.
 *
 * Model costs are frequently sub-cent, and `$0.00` next to a bot that just
 * did work reads like a bug. Below a cent this falls back to four decimals,
 * and below that to `<$0.0001`.
 */
export function formatCostUsd(amountUsd: number): string {
  if (!Number.isFinite(amountUsd) || amountUsd <= 0) return "$0.00"
  if (amountUsd < 0.0001) return "<$0.0001"
  if (amountUsd < 0.01) return `$${amountUsd.toFixed(4)}`
  if (amountUsd < 1) return `$${amountUsd.toFixed(3)}`
  return `$${amountUsd.toFixed(2)}`
}

/** Sum a set of ledger-shaped rows. */
export function sumCostUsd(entries: readonly { cost_usd: number }[]): number {
  return entries.reduce((total, entry) => total + (entry.cost_usd || 0), 0)
}
