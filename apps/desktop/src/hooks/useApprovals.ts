/**
 * The approvals queue, decided optimistically.
 *
 * ## Why the decision is applied before the server answers
 *
 * `POST /approvals/{id}/decide` is not a cheap write. On `approved` the API
 * executes the held action *and then resumes the parked run* — it rebuilds the
 * conversation, replays the step record and carries the task on. The response
 * comes back when all of that has happened. So the honest round trip for the
 * one gesture a person makes most often in this product is seconds, not
 * milliseconds, and the pre-existing behaviour was to sit on a spinner for the
 * whole of it while the queue still said the item was waiting.
 *
 * The decision itself, though, is not in doubt the moment it is pressed. It is
 * the *execution* that is uncertain. So the two are separated: the row flips to
 * approved/rejected immediately (which also drops the sidebar badge and the
 * queue summary), and what the action then did arrives when it arrives.
 *
 * ## Getting the rollback right
 *
 * An optimistic update that lies is worse than a spinner, so this keeps the
 * exact row it overwrote and puts it back on any failure. The failure that is
 * not hypothetical is **409**: deciding a non-pending approval — someone
 * decided it on their phone, or the sweeper expired it — and losing that race
 * must not leave the queue showing a decision that never happened. So a
 * conflict rolls back *and* refetches, because in that case the server knows
 * something this client does not, and guessing at it would be a second lie.
 */
import { useCallback, useMemo, useRef, useState } from "react"
import * as api from "../api/endpoints"
import { isApiError } from "../api/client"
import { useAsyncResource } from "./useAsync"
import type { Approval, ApprovalDecisionResult } from "../types"

export type Decision = "approved" | "rejected"

/**
 * A decision this client has shown but the server has not confirmed.
 *
 * `lost` is set when the round trip came back 409. The row has already been
 * rolled back by then; the flag is what lets the card say *why* it moved back
 * rather than simply flickering.
 */
export interface OptimisticDecision {
  decision: Decision
  at: number
  lost?: boolean
}

export interface ApprovalsApi {
  approvals: Approval[]
  pending: Approval[]
  pendingCount: number
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  /** Which approval ids currently have a decision in flight. */
  deciding: Record<string, boolean>
  /** Decisions shown ahead of the server. Keyed by approval id. */
  optimistic: Record<string, OptimisticDecision>
  decide: (id: string, decision: Decision, note?: string) => Promise<ApprovalDecisionResult>
  expire: (id: string) => Promise<void>
  statusFilter: string
  setStatusFilter: (status: string) => void
  botFilter: string | null
  setBotFilter: (botId: string | null) => void
}

export function useApprovals(initialStatus = "pending"): ApprovalsApi {
  const [statusFilter, setStatusFilter] = useState(initialStatus)
  const [botFilter, setBotFilter] = useState<string | null>(null)
  const [deciding, setDeciding] = useState<Record<string, boolean>>({})
  const [optimistic, setOptimistic] = useState<Record<string, OptimisticDecision>>({})

  const resource = useAsyncResource<Approval[]>(
    (signal) =>
      api.listApprovals(
        {
          status: statusFilter === "all" ? undefined : statusFilter,
          bot_id: botFilter ?? undefined,
        },
        signal,
      ),
    [statusFilter, botFilter],
    { initialData: [] },
  )
  const { setData, refetch } = resource

  /*
   * The row as it stands right now, for the rollback snapshot.
   *
   * Read from a ref rather than from inside the `setData` updater: an updater
   * is not a place to capture values out of (React may invoke it twice, and it
   * runs later than the call site), and the snapshot has to be taken *before*
   * the optimistic write, not during it.
   */
  const dataRef = useRef<Approval[]>(resource.data)
  dataRef.current = resource.data

  const pending = useMemo(() => resource.data.filter((a) => a.status === "pending"), [resource.data])

  const forget = useCallback((id: string) => {
    setDeciding((prev) => {
      const next = { ...prev }
      delete next[id]
      return next
    })
  }, [])

  const decide = useCallback(
    async (id: string, decision: Decision, note?: string) => {
      const trimmed = note?.trim() || undefined
      const snapshot = dataRef.current.find((a) => a.id === id) ?? null

      setDeciding((prev) => ({ ...prev, [id]: true }))
      setOptimistic((prev) => ({ ...prev, [id]: { decision, at: Date.now() } }))
      // The optimistic write. `status` is the field everything else reads —
      // the queue summary, the sidebar badge, the card's own decided styling —
      // so writing it here is what makes the whole app move at once.
      setData((prev) =>
        prev.map((a) =>
          a.id === id
            ? { ...a, status: decision, decided_at: new Date().toISOString(), note: trimmed ?? a.note }
            : a,
        ),
      )

      try {
        const result = await api.decideApproval(id, { decision, note: trimmed })
        setData((prev) => prev.map((a) => (a.id === id ? { ...a, ...result } : a)))
        setOptimistic((prev) => {
          const next = { ...prev }
          delete next[id]
          return next
        })
        return result
      } catch (err) {
        // Put the row back exactly as it was. Never a reconstructed "pending"
        // row: the one we replaced is the only version known to be true.
        if (snapshot) setData((prev) => prev.map((a) => (a.id === id ? snapshot : a)))
        const lost = isApiError(err) && err.isConflict
        setOptimistic((prev) => ({ ...prev, [id]: { decision, at: Date.now(), lost } }))
        // A 409 means the server has a version of this row we do not. Go and
        // get it rather than leaving the rolled-back guess on screen.
        if (lost) void refetch()
        throw err
      } finally {
        forget(id)
      }
    },
    [setData, refetch, forget],
  )

  const expire = useCallback(
    async (id: string) => {
      const result = await api.expireApproval(id)
      setData((prev) => prev.map((a) => (a.id === id ? { ...a, ...result } : a)))
    },
    [setData],
  )

  return {
    approvals: resource.data,
    pending,
    pendingCount: pending.length,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch,
    deciding,
    optimistic,
    decide,
    expire,
    statusFilter,
    setStatusFilter,
    botFilter,
    setBotFilter,
  }
}
