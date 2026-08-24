import { useCallback, useMemo } from "react"
import * as api from "../api/endpoints"
import { useAsyncResource } from "./useAsync"
import type { Bot, CreateBotInput, UpdateBotInput } from "../types"

export interface BotsApi {
  bots: Bot[]
  byId: Record<string, Bot>
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  createBot: (input: CreateBotInput) => Promise<Bot>
  updateBot: (botId: string, input: UpdateBotInput) => Promise<Bot>
  deleteBot: (botId: string) => Promise<void>
  setBudget: (botId: string, dailyBudgetUsd: number) => Promise<void>
}

export function useBots(): BotsApi {
  const resource = useAsyncResource<Bot[]>((signal) => api.listBots(signal), [], { initialData: [] })
  const { setData, refetch } = resource

  const byId = useMemo(() => {
    const map: Record<string, Bot> = {}
    for (const bot of resource.data) map[bot.id] = bot
    return map
  }, [resource.data])

  const createBot = useCallback(
    async (input: CreateBotInput) => {
      const bot = await api.createBot(input)
      setData((prev) => [...prev, bot])
      return bot
    },
    [setData],
  )

  const updateBot = useCallback(
    async (botId: string, input: UpdateBotInput) => {
      const bot = await api.updateBot(botId, input)
      setData((prev) => prev.map((b) => (b.id === botId ? { ...b, ...bot } : b)))
      return bot
    },
    [setData],
  )

  const deleteBot = useCallback(
    async (botId: string) => {
      await api.deleteBot(botId)
      setData((prev) => prev.filter((b) => b.id !== botId))
    },
    [setData],
  )

  const setBudget = useCallback(
    async (botId: string, dailyBudgetUsd: number) => {
      // Returns the full BotOut, so trust it over the value we sent.
      const bot = await api.updateBudget(botId, { daily_budget_usd: dailyBudgetUsd })
      setData((prev) => prev.map((b) => (b.id === botId ? { ...b, ...bot } : b)))
    },
    [setData],
  )

  return {
    bots: resource.data,
    byId,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch,
    createBot,
    updateBot,
    deleteBot,
    setBudget,
  }
}
