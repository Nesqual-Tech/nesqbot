/**
 * Standing permissions — the list, and one call to take one back.
 *
 * ## Why this is not folded into `useApprovals`
 *
 * They look like the same thing and they answer opposite questions. The queue
 * is *what is waiting on you*: it polls, it is the sidebar badge, and every row
 * in it is a decision you have not made. This is *what you have already
 * allowed*: it changes only when you grant or revoke one, and its whole reason
 * to exist is that a permission acquired automatically has to be findable
 * afterwards. Polling it would be spending a request every few seconds on a
 * list that almost never moves.
 *
 * ## Why revoking is not optimistic
 *
 * The decision hook applies a decision before the server confirms it, because
 * a decision is not in doubt the moment it is pressed and the round trip is
 * seconds long. Revoking is the opposite case on both counts: it is a single
 * cheap `UPDATE`, and what a person needs from it is certainty that the
 * permission is gone. Showing "revoked" ahead of the server and rolling it back
 * on failure would be, for one frame, a UI that says a bot cannot do something
 * it can. So this waits, and the row moves when it has actually moved.
 */
import { useCallback, useState } from "react"
import * as api from "../api/endpoints"
import { useAsyncResource } from "./useAsync"
import type { StandingApproval } from "../types"

export interface StandingApprovalsApi {
  rules: StandingApproval[]
  /** The limit that cannot be switched off, in the server's own words. */
  alwaysAsks: string
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  /** Which rule ids have a revoke in flight. */
  revoking: Record<string, boolean>
  revoke: (id: string) => Promise<void>
  includeRevoked: boolean
  setIncludeRevoked: (value: boolean) => void
}

export function useStandingApprovals(): StandingApprovalsApi {
  const [includeRevoked, setIncludeRevoked] = useState(false)
  const [revoking, setRevoking] = useState<Record<string, boolean>>({})

  const resource = useAsyncResource(
    (signal) => api.listStandingApprovals({ include_revoked: includeRevoked || undefined }, signal),
    [includeRevoked],
    { initialData: { items: [], always_asks: "" } },
  )
  const { setData, refetch } = resource

  const revoke = useCallback(
    async (id: string) => {
      setRevoking((prev) => ({ ...prev, [id]: true }))
      try {
        const revoked = await api.revokeStandingApproval(id)
        setData((prev) => ({
          ...prev,
          // Revoked rules leave the default list entirely — the question it
          // answers is "what can my bots do without asking", and a revoked row
          // is not an answer to it. `includeRevoked` is where the record lives.
          items: includeRevoked
            ? prev.items.map((rule) => (rule.id === id ? revoked : rule))
            : prev.items.filter((rule) => rule.id !== id),
        }))
      } finally {
        setRevoking((prev) => {
          const next = { ...prev }
          delete next[id]
          return next
        })
      }
    },
    [setData, includeRevoked],
  )

  return {
    rules: resource.data.items,
    alwaysAsks: resource.data.always_asks,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch,
    revoking,
    revoke,
    includeRevoked,
    setIncludeRevoked,
  }
}
