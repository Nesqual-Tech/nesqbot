/**
 * The inbox: everything that is waiting on this person, in one store.
 *
 * Two different things park an agent, and from the phone's point of view they
 * are the same job — something stopped and only you can restart it:
 *
 *  - a **held action** waiting for approve or reject (`GET /approvals`);
 *  - a **run handed to a human** waiting for Continue
 *    (`GET /runs?status=awaiting_human`).
 *
 * They are kept in one external store rather than in each screen's state for
 * the reason the approvals cache already existed: the tab badge and the list
 * have to agree instantly. Deciding an approval, or resuming a run, must drop
 * the badge without waiting for the next poll.
 *
 * **Partial failure is a first-class state.** The two fetches are independent
 * and either can fail on its own — most often because an older API build has no
 * `/runs` visibility for this user. A failed approvals fetch must not blank the
 * takeovers that did load, so each half keeps its own error and its own last
 * good value.
 */
import { useSyncExternalStore } from "react"
import type { Approval } from "../api/types"
import { approvals as approvalsApi, runs as runsApi } from "../api/endpoints"
import { mergeTakeovers, takeoverFromRun, type TakeoverRequest } from "../lib/takeover"

export interface InboxSnapshot {
  approvals: Approval[]
  takeovers: TakeoverRequest[]
  /** Approvals + takeovers. What the tab badge counts. */
  count: number
  /** Set when the approvals fetch failed. The list may still be the last good one. */
  approvalsError: unknown
  /** Set when the parked-runs fetch failed. */
  takeoversError: unknown
  /** Epoch ms of the last fetch where at least one half succeeded. */
  loadedAt: number | null
}

const EMPTY: InboxSnapshot = {
  approvals: [],
  takeovers: [],
  count: 0,
  approvalsError: null,
  takeoversError: null,
  loadedAt: null,
}

/**
 * One frozen snapshot object, replaced wholesale on every change.
 *
 * `useSyncExternalStore` compares snapshots by reference and will loop forever
 * if `getSnapshot` returns a fresh object each call, so the object is built
 * here on write and merely handed out on read.
 */
let snapshot: InboxSnapshot = EMPTY
const listeners = new Set<() => void>()

function emit(next: Omit<InboxSnapshot, "count">): void {
  snapshot = { ...next, count: next.approvals.length + next.takeovers.length }
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function getSnapshot(): InboxSnapshot {
  return snapshot
}

function getCount(): number {
  return snapshot.count
}

export function getInbox(): InboxSnapshot {
  return snapshot
}

export function setPendingApprovals(next: Approval[]): void {
  emit({ ...snapshot, approvals: next, approvalsError: null, loadedAt: Date.now() })
}

/**
 * Drop a decided approval immediately.
 *
 * Called from the detail screen the moment the API answers, so the badge and
 * the list are right before the next poll — a person who has just approved
 * something and still sees it queued reasonably assumes it did not go through.
 */
export function removePendingApproval(id: string): void {
  const next = snapshot.approvals.filter((item) => item.id !== id)
  if (next.length !== snapshot.approvals.length) emit({ ...snapshot, approvals: next })
}

/** Add or refresh one takeover — used by the live SSE frame. */
export function upsertTakeover(request: TakeoverRequest): void {
  emit({ ...snapshot, takeovers: mergeTakeovers(snapshot.takeovers, [request]) })
}

/** Drop a resumed run. Same immediacy argument as `removePendingApproval`. */
export function removeTakeover(runId: string): void {
  const next = snapshot.takeovers.filter((item) => item.runId !== runId)
  if (next.length !== snapshot.takeovers.length) emit({ ...snapshot, takeovers: next })
}

export function clearInbox(): void {
  emit(EMPTY)
}

/**
 * Fetch both halves.
 *
 * Never rejects: the inbox is polled from the tab bar, where a thrown error
 * would be an unhandled rejection on a timer. Failures land on the snapshot,
 * where a screen can render them, and the caller gets the snapshot back.
 *
 * `Promise.allSettled` rather than `all` is the whole point — `all` would make
 * one failing half discard the other's result.
 */
export async function refreshInbox(signal?: AbortSignal): Promise<InboxSnapshot> {
  const [approvalsResult, runsResult] = await Promise.allSettled([
    approvalsApi.list({ status: "pending" }, { signal }),
    runsApi.parked({ signal }),
  ])

  const approvals = approvalsResult.status === "fulfilled" ? approvalsResult.value : snapshot.approvals
  const approvalsError = approvalsResult.status === "rejected" ? approvalsResult.reason : null

  let takeovers = snapshot.takeovers
  let takeoversError: unknown = null
  if (runsResult.status === "fulfilled") {
    const parked = runsResult.value.map(takeoverFromRun).filter((item): item is TakeoverRequest => item !== null)
    // Replace rather than merge: a run missing from the poll has been resumed
    // or has moved on, and keeping a live card for it would offer a Continue
    // button that answers `resumed: false` forever.
    takeovers = mergeTakeovers([], parked)
  } else {
    takeoversError = runsResult.reason
  }

  const anySucceeded = approvalsResult.status === "fulfilled" || runsResult.status === "fulfilled"
  emit({
    approvals,
    takeovers,
    approvalsError,
    takeoversError,
    loadedAt: anySucceeded ? Date.now() : snapshot.loadedAt,
  })
  return snapshot
}

export function useInbox(): InboxSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** Just the badge number, so the tab bar does not re-render on list churn. */
export function useInboxCount(): number {
  return useSyncExternalStore(subscribe, getCount, getCount)
}
