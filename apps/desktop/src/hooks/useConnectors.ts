/** Connector catalog + per-bot bindings. */
import { useCallback, useMemo } from "react"
import * as api from "../api/endpoints"
import { useAsyncResource } from "./useAsync"
import type {
  BindConnectorInput,
  Connector,
  ConnectorActionOutcome,
  ConnectorBinding,
  RegisterConnectorInput,
} from "../types"

export interface ConnectorsApi {
  connectors: Connector[]
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  registerConnector: (input: RegisterConnectorInput) => Promise<Connector>
  deleteConnector: (connectorId: string) => Promise<void>

  bindings: Record<string, ConnectorBinding>
  bindingsLoading: boolean
  bindingsError: unknown
  refetchBindings: () => Promise<void>
  bind: (connectorId: string, input: BindConnectorInput) => Promise<void>
  unbind: (connectorId: string) => Promise<void>
  /**
   * Executes a bound connector action. Risk-gated actions resolve to a
   * `PendingApprovalOut` rather than a result — narrow with
   * `isPendingApproval` before reading the outcome.
   */
  runAction: (connectorId: string, action: string, input: Record<string, unknown>) => Promise<ConnectorActionOutcome>
}

export function useConnectors(botId: string | null): ConnectorsApi {
  const catalog = useAsyncResource<Connector[]>((signal) => api.listConnectors(signal), [], {
    initialData: [],
  })

  const bindingList = useAsyncResource<ConnectorBinding[]>(
    (signal) => (botId ? api.listBotConnectors(botId, signal) : Promise.resolve([])),
    [botId],
    { initialData: [], enabled: Boolean(botId) },
  )

  const bindings = useMemo(() => {
    const map: Record<string, ConnectorBinding> = {}
    for (const binding of bindingList.data ?? []) {
      if (binding.connector_id) map[binding.connector_id] = binding
    }
    return map
  }, [bindingList.data])

  const registerConnector = useCallback(
    async (input: RegisterConnectorInput) => {
      const created = await api.registerConnector(input)
      catalog.setData((prev) => [...prev.filter((c) => c.id !== created.id), created])
      return created
    },
    [catalog],
  )

  const deleteConnector = useCallback(
    async (connectorId: string) => {
      await api.deleteConnector(connectorId)
      catalog.setData((prev) => prev.filter((c) => c.id !== connectorId))
    },
    [catalog],
  )

  const bind = useCallback(
    async (connectorId: string, input: BindConnectorInput) => {
      if (!botId) throw new Error("Select a bot first")
      const result = await api.bindConnector(botId, connectorId, input)
      bindingList.setData((prev) => {
        const existing = prev.find((b) => b.connector_id === connectorId)
        // A binding row is denormalised from the catalog, so fill the display
        // fields from the connector rather than shipping a half-built row.
        const connector = catalog.data.find((item) => item.id === connectorId)
        const row: ConnectorBinding = {
          bot_id: botId,
          connector_id: connectorId,
          name: existing?.name ?? connector?.name ?? connectorId,
          status: result?.status ?? input.status ?? "connected",
          secret_ref: input.secret_ref ?? null,
          risk_default: existing?.risk_default ?? connector?.risk_default ?? "observe",
          first_party: existing?.first_party ?? connector?.first_party ?? false,
          actions: existing?.actions ?? connector?.actions ?? [],
        }
        return [...prev.filter((b) => b.connector_id !== connectorId), row]
      })
    },
    [botId, bindingList, catalog.data],
  )

  const unbind = useCallback(
    async (connectorId: string) => {
      if (!botId) throw new Error("Select a bot first")
      await api.unbindConnector(botId, connectorId)
      bindingList.setData((prev) => prev.filter((b) => b.connector_id !== connectorId))
    },
    [botId, bindingList],
  )

  const runAction = useCallback(
    async (connectorId: string, action: string, input: Record<string, unknown>) => {
      if (!botId) throw new Error("Select a bot first")
      return api.executeConnectorAction(botId, connectorId, action, input)
    },
    [botId],
  )

  return {
    connectors: catalog.data,
    loading: catalog.loading,
    initialising: catalog.initialising,
    error: catalog.error,
    refetch: catalog.refetch,
    registerConnector,
    deleteConnector,
    bindings,
    bindingsLoading: bindingList.loading,
    bindingsError: bindingList.error,
    refetchBindings: bindingList.refetch,
    bind,
    unbind,
    runAction,
  }
}
