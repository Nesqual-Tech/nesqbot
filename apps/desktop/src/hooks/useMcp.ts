/** MCP server registry, tool browsing and per-bot attachment. */
import { useCallback, useState } from "react"
import * as api from "../api/endpoints"
import { errorMessage } from "../api/client"
import { useAsyncResource } from "./useAsync"
import type { McpServer, McpTool, RegisterMcpInput, UpdateMcpInput } from "../types"

export interface McpToolsState {
  loading: boolean
  error: string | null
  mock: boolean
  tools: McpTool[]
}

export interface McpApi {
  servers: McpServer[]
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  register: (input: RegisterMcpInput) => Promise<McpServer>
  update: (id: string, input: UpdateMcpInput) => Promise<McpServer>
  remove: (id: string) => Promise<void>
  /** Tool listings keyed by server id, populated on demand. */
  tools: Record<string, McpToolsState>
  loadTools: (id: string) => Promise<void>
  /** Attachment state is client-side: the API has no "list attachments" route. */
  attached: Record<string, boolean>
  attach: (botId: string, mcpId: string) => Promise<void>
  detach: (botId: string, mcpId: string) => Promise<void>
  callTool: (botId: string, mcpId: string, tool: string, args: Record<string, unknown>) => Promise<unknown>
}

export function useMcp(): McpApi {
  const resource = useAsyncResource<McpServer[]>((signal) => api.listMcp(signal), [], { initialData: [] })
  const { setData } = resource
  const [tools, setTools] = useState<Record<string, McpToolsState>>({})
  const [attached, setAttached] = useState<Record<string, boolean>>({})

  const register = useCallback(
    async (input: RegisterMcpInput) => {
      const server = await api.registerMcp(input)
      setData((prev) => [...prev.filter((s) => s.id !== server.id), server])
      return server
    },
    [setData],
  )

  const update = useCallback(
    async (id: string, input: UpdateMcpInput) => {
      const server = await api.updateMcp(id, input)
      setData((prev) => prev.map((s) => (s.id === id ? { ...s, ...server } : s)))
      return server
    },
    [setData],
  )

  const remove = useCallback(
    async (id: string) => {
      await api.deleteMcp(id)
      setData((prev) => prev.filter((s) => s.id !== id))
      setTools((prev) => {
        const next = { ...prev }
        delete next[id]
        return next
      })
    },
    [setData],
  )

  const loadTools = useCallback(async (id: string) => {
    setTools((prev) => ({
      ...prev,
      [id]: { loading: true, error: null, mock: prev[id]?.mock ?? false, tools: prev[id]?.tools ?? [] },
    }))
    try {
      const result = await api.listMcpTools(id)
      setTools((prev) => ({
        ...prev,
        [id]: {
          loading: false,
          error: result.error ?? null,
          mock: Boolean(result.mock),
          tools: result.tools,
        },
      }))
    } catch (err) {
      setTools((prev) => ({
        ...prev,
        [id]: { loading: false, error: errorMessage(err), mock: false, tools: [] },
      }))
    }
  }, [])

  const attach = useCallback(async (botId: string, mcpId: string) => {
    await api.attachMcp(botId, mcpId)
    setAttached((prev) => ({ ...prev, [`${botId}:${mcpId}`]: true }))
  }, [])

  const detach = useCallback(async (botId: string, mcpId: string) => {
    await api.detachMcp(botId, mcpId)
    setAttached((prev) => ({ ...prev, [`${botId}:${mcpId}`]: false }))
  }, [])

  const callTool = useCallback(
    (botId: string, mcpId: string, tool: string, args: Record<string, unknown>) =>
      api.callMcpTool(botId, mcpId, { tool, arguments: args }),
    [],
  )

  return {
    servers: resource.data,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch: resource.refetch,
    register,
    update,
    remove,
    tools,
    loadTools,
    attached,
    attach,
    detach,
    callTool,
  }
}
