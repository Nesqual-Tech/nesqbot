import { useCallback, useMemo } from "react"
import * as api from "../api/endpoints"
import { useAsyncResource } from "./useAsync"
import type { ModelTier, UsageRow } from "../types"

export interface TierBreakdown {
  tier: ModelTier | string
  calls: number
  inputTokens: number
  outputTokens: number
  costUsd: number
}

export interface UsageApi {
  rows: UsageRow[]
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  totalSpend: number
  totalBudget: number
  setBudget: (botId: string, dailyBudgetUsd: number) => Promise<void>
  breakdown: (row: UsageRow) => TierBreakdown[]
}

const TIER_ORDER: Record<string, number> = { nano: 0, mini: 1, reason: 2, embed: 3 }

export function useUsage(days = 1): UsageApi {
  const resource = useAsyncResource<UsageRow[]>((signal) => api.getUsage(days, signal), [days], {
    initialData: [],
  })
  const { setData, refetch } = resource

  const totals = useMemo(() => {
    let spend = 0
    let budget = 0
    for (const row of resource.data) {
      spend += Number(row.spent_usd_today) || 0
      budget += Number(row.budget_usd) || 0
    }
    return { spend, budget }
  }, [resource.data])

  const setBudget = useCallback(
    async (botId: string, dailyBudgetUsd: number) => {
      // Returns the full BotOut — use its budget rather than refetching /usage.
      const bot = await api.updateBudget(botId, { daily_budget_usd: dailyBudgetUsd })
      setData((prev) =>
        prev.map((r) => (r.bot_id === botId ? { ...r, budget_usd: bot.daily_budget_usd ?? dailyBudgetUsd } : r)),
      )
    },
    [setData],
  )

  const breakdown = useCallback((row: UsageRow): TierBreakdown[] => {
    const map = new Map<string, TierBreakdown>()
    for (const entry of row.entries ?? []) {
      const tier = String(entry.tier)
      const bucket = map.get(tier) ?? {
        tier,
        calls: 0,
        inputTokens: 0,
        outputTokens: 0,
        costUsd: 0,
      }
      bucket.calls += 1
      bucket.inputTokens += Number(entry.input_tokens) || 0
      bucket.outputTokens += Number(entry.output_tokens) || 0
      bucket.costUsd += Number(entry.cost_usd) || 0
      map.set(tier, bucket)
    }
    return [...map.values()].sort(
      (a, b) => (TIER_ORDER[a.tier] ?? 99) - (TIER_ORDER[b.tier] ?? 99) || b.costUsd - a.costUsd,
    )
  }, [])

  return {
    rows: resource.data,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch,
    totalSpend: totals.spend,
    totalBudget: totals.budget,
    setBudget,
    breakdown,
  }
}
