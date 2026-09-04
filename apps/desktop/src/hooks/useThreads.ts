import { useCallback } from "react"
import * as api from "../api/endpoints"
import { useAsyncResource } from "./useAsync"
import type { Bot, CreateThreadInput, Thread, UpdateThreadInput } from "../types"

export interface ThreadsApi {
  threads: Thread[]
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  createThread: (input: CreateThreadInput) => Promise<Thread>
  deleteThread: (threadId: string) => Promise<void>
  /** Rename and/or pin. Pinned threads float to the top of the list. */
  updateThread: (threadId: string, patch: UpdateThreadInput) => Promise<Thread>
  /** Existing 1:1 thread for the bot, or a freshly created one. */
  ensureThreadForBot: (bot: Bot) => Promise<Thread>
  /** Seat more bots on a thread — what turns a 1:1 into a group. */
  addBots: (threadId: string, botIds: string[]) => Promise<Thread>
  removeBot: (threadId: string, botId: string) => Promise<Thread>
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

  const updateThread = useCallback(
    async (threadId: string, patch: UpdateThreadInput) => {
      const thread = await api.updateThread(threadId, patch)
      // Re-sort locally the way `GET /threads` does — pinned first, then by
      // `updated_at` — so a pin takes effect without a refetch.
      setData((prev) =>
        prev
          .map((t) => (t.id === thread.id ? thread : t))
          .sort(
            (a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) || b.updated_at.localeCompare(a.updated_at),
          ),
      )
      return thread
    },
    [setData],
  )

  const addBots = useCallback(
    async (threadId: string, botIds: string[]) => {
      const thread = await api.addThreadBots(threadId, botIds)
      setData((prev) => prev.map((t) => (t.id === thread.id ? thread : t)))
      return thread
    },
    [setData],
  )

  const removeBot = useCallback(
    async (threadId: string, botId: string) => {
      const thread = await api.removeThreadBot(threadId, botId)
      setData((prev) => prev.map((t) => (t.id === thread.id ? thread : t)))
      return thread
    },
    [setData],
  )

  const threadsForBot = useCallback((botId: string) => threads.filter((t) => t.bot_ids?.includes(botId)), [threads])

  const ensureThreadForBot = useCallback(
    async (bot: Bot) => {
      /*
       * Any thread this bot is in, not only a thread where it is *alone*.
       *
       * The old predicate was `bot_ids.length === 1 && bot_ids[0] === bot.id`,
       * which no group thread can ever satisfy. Two consequences, both seen in
       * a screenshot of the running app: a group conversation was skipped over
       * in favour of creating a brand new empty 1:1, and every launch left
       * another empty thread behind. Group threads were second-class in the
       * one place that decides which conversation you land in.
       *
       * `threads` arrives newest-first (`list_threads` orders by
       * `updated_at desc`) and `createThread` prepends, so the first match is
       * the conversation most recently spoken in - which is the one a person
       * means by "my chat with this bot". The picker still switches between
       * them.
       */
      const existing = threads.find((t) => t.bot_ids?.includes(bot.id))
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
    updateThread,
    ensureThreadForBot,
    addBots,
    removeBot,
    threadsForBot,
  }
}
