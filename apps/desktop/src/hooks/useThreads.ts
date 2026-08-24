import { useCallback } from "react"
import * as api from "../api/endpoints"
import { useAsyncResource } from "./useAsync"
import type { Bot, CreateThreadInput, Thread } from "../types"

export interface ThreadsApi {
  threads: Thread[]
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  createThread: (input: CreateThreadInput) => Promise<Thread>
  deleteThread: (threadId: string) => Promise<void>
  /** Existing 1:1 thread for the bot, or a freshly created one. */
  ensureThreadForBot: (bot: Bot) => Promise<Thread>
  threadsForBot: (botId: string) => Thread[]
}

export function useThreads(): ThreadsApi {
  const resource = useAsyncResource<Thread[]>((signal) => api.listThreads(signal), [], { initialData: [] })
  const { data: threads, setData } = resource

  const createThread = useCallback(
    async (input: CreateThreadInput) => {
      const thread = await api.createThread(input)
      setData((prev) => [thread, ...prev.filter((t) => t.id !== thread.id)])
      return thread
    },
    [setData],
  )

  const deleteThread = useCallback(
    async (threadId: string) => {
      await api.deleteThread(threadId)
      setData((prev) => prev.filter((t) => t.id !== threadId))
    },
    [setData],
  )

  const threadsForBot = useCallback((botId: string) => threads.filter((t) => t.bot_ids?.includes(botId)), [threads])

  const ensureThreadForBot = useCallback(
    async (bot: Bot) => {
      const existing = threads.find((t) => t.bot_ids?.length === 1 && t.bot_ids[0] === bot.id)
      if (existing) return existing
      return createThread({ bot_ids: [bot.id], title: bot.name })
    },
    [threads, createThread],
  )

  return {
    threads,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch: resource.refetch,
    createThread,
    deleteThread,
    ensureThreadForBot,
    threadsForBot,
  }
}
