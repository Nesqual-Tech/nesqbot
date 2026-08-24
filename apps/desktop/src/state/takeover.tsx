/**
 * Human handoff, app-wide.
 *
 * A takeover can be raised on any thread and has to be answerable from
 * anywhere — including after the app has been closed and reopened — so it
 * cannot live inside the chat pane that happened to be watching the stream.
 * This context is the one place that knows which runs are parked, and the one
 * place that calls `POST /runs/{id}/resume`.
 *
 * Two sources feed it, and they are not redundant:
 *
 *  - **the `takeover` SSE event**, pushed into `raise()` by whatever channel is
 *    open. Instant, and carries the bot's own explanation.
 *  - **`GET /runs?status=awaiting_human`**, polled. This is the durable one: a
 *    parked run outlives the process, and the stream only ever covers the
 *    thread that is currently on screen. A takeover raised on another thread,
 *    or before this window existed, arrives here.
 *
 * # The one invariant
 *
 * Resume must be sent **once** per press, no matter how fast the pointer is.
 * The endpoint is idempotent server-side, but "the server sorts it out" is not
 * a UI answer — a second POST is a second round trip whose `resumed: false`
 * would have to be explained away. So the guard is a `Set` in a ref, checked
 * and written synchronously before the first `await`. A disabled button is the
 * second layer, not the first: `disabled` follows a state update, and two
 * clicks in one frame both read the pre-update value.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import * as api from "../api/endpoints"
import { ApiError, REQUIRES_SIGN_IN, errorMessage } from "../api/client"
import { useAuth } from "../auth"
import {
  applyRememberedCopy,
  forgetCopy,
  mergeRequest,
  rememberCopy,
  requestFromRun,
  type TakeoverRequest,
} from "../lib/takeover"
import type { ResumeRunOut } from "../types"

/** How often the parked-run list is re-checked. */
const POLL_MS = 45_000

/**
 * How long a resolved run is refused re-entry.
 *
 * `resume` returns before the run's status has necessarily settled everywhere,
 * and the refresh that follows it can still see the row as `awaiting_human`.
 * Without this the card the user just answered blinks straight back, which
 * reads as "it did not work" at precisely the moment the product has to look
 * like it did.
 */
const RESOLVED_GRACE_MS = 30_000

export type ResumeOutcome =
  | { kind: "resumed"; result: ResumeRunOut }
  /** `resumed: false` — someone (or something) already started it. Not a failure. */
  | { kind: "already-running"; result: ResumeRunOut }
  /** The synchronous guard tripped: this press was a duplicate, nothing was sent. */
  | { kind: "ignored" }
  | { kind: "failed"; error: string; code: string | null; gone: boolean }

export interface TakeoverContextValue {
  /** Parked runs, newest first. Dismissed ones are still in here. */
  requests: TakeoverRequest[]
  /** Runs the person has pushed aside this session — still findable, not in their face. */
  dismissed: ReadonlySet<string>
  /** Everything for one bot, dismissed or not. */
  forBot: (botId: string | null | undefined) => TakeoverRequest[]
  byRunId: (runId: string) => TakeoverRequest | null
  /**
   * The newest live arrival the shell has not yet reacted to. Consumed by the
   * shell, which selects the bot and maximises the desktop, then calls
   * `markPresented`. Deliberately not "the newest request": a run recovered
   * from the poll on a cold start must not hijack the window.
   */
  unpresented: TakeoverRequest | null
  markPresented: (runId: string) => void
  /** Feed a parsed `takeover` frame in. Idempotent per run. */
  raise: (request: TakeoverRequest) => void
  /** The run is no longer parked — it resumed, finished, or was cancelled. */
  clear: (runId: string) => void
  dismiss: (runId: string) => void
  undismiss: (runId: string) => void
  /** Send `resume` exactly once. Safe to call twice; the second call is `ignored`. */
  resume: (runId: string, note?: string) => Promise<ResumeOutcome>
  isResuming: (runId: string) => boolean
  refresh: () => Promise<void>
  /**
   * Last transport failure on the parked-run poll.
   *
   * Deliberately not rendered anywhere. The live `takeover` event is the
   * primary path and this poll is the safety net; a build talking to an API
   * that predates `awaiting_human` gets a 404 here every 45 seconds, and
   * turning that into a visible error would be a permanent banner about a
   * feature the user cannot see is missing. Exposed so a diagnostics surface
   * can read it without the provider having to grow one.
   */
  loadError: string | null
}

const TakeoverContext = createContext<TakeoverContextValue | null>(null)

function sortRequests(list: TakeoverRequest[]): TakeoverRequest[] {
  return [...list].sort((a, b) => b.raisedAt - a.raisedAt)
}

export function TakeoverProvider({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const [requests, setRequests] = useState<TakeoverRequest[]>([])
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set())
  const [presented, setPresented] = useState<Set<string>>(() => new Set())
  const [resuming, setResuming] = useState<Set<string>>(() => new Set())
  const [loadError, setLoadError] = useState<string | null>(null)

  /*
   * The guard. A ref, not state, and written before anything asynchronous
   * happens — see the note at the top of the file.
   */
  const inFlight = useRef<Set<string>>(new Set())
  /** runId → when it was resolved locally. See `RESOLVED_GRACE_MS`. */
  const resolved = useRef<Map<string, number>>(new Map())
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // A dev build has no session to wait for; a packaged one must not poll a
  // protected route before there is a token, or the first thing the user sees
  // is a 401 they cannot act on.
  const enabled = !REQUIRES_SIGN_IN || status === "authenticated"

  /** True while a locally resolved run is still inside its grace window. */
  const isRecentlyResolved = useCallback((runId: string): boolean => {
    const at = resolved.current.get(runId)
    if (at === undefined) return false
    if (Date.now() - at < RESOLVED_GRACE_MS) return true
    resolved.current.delete(runId)
    return false
  }, [])

  /*
   * The disk cache is kept in step from an effect, not from inside the state
   * updaters. A `setState` callback can be invoked more than once for the same
   * transition — React does exactly that in StrictMode — so writing
   * `localStorage` from in there is a side effect in a function contracted to
   * be pure. Diffing here instead means one write per settled state.
   */
  const remembered = useRef<Set<string>>(new Set())
  useEffect(() => {
    const live = new Set<string>()
    for (const request of requests) {
      live.add(request.runId)
      rememberCopy(request)
    }
    for (const runId of remembered.current) if (!live.has(runId)) forgetCopy(runId)
    remembered.current = live
  }, [requests])

  const upsert = useCallback((incoming: TakeoverRequest) => {
    // A fresh `takeover` for a run we just resolved is a genuinely new
    // interruption — the agent carried on and hit a second login. Let it back.
    resolved.current.delete(incoming.runId)
    setRequests((prev) => {
      const existing = prev.find((r) => r.runId === incoming.runId)
      const merged = mergeRequest(existing, incoming)
      return sortRequests(existing ? prev.map((r) => (r.runId === merged.runId ? merged : r)) : [...prev, merged])
    })
  }, [])

  const clear = useCallback((runId: string) => {
    resolved.current.set(runId, Date.now())
    setRequests((prev) => (prev.some((r) => r.runId === runId) ? prev.filter((r) => r.runId !== runId) : prev))
    setDismissed((prev) => {
      if (!prev.has(runId)) return prev
      const next = new Set(prev)
      next.delete(runId)
      return next
    })
  }, [])

  /**
   * Reconcile against the server list.
   *
   * The server is authoritative about membership, so a run it no longer
   * reports is dropped — with one exception: a run whose resume is in flight
   * right now. That call is what takes it out of `awaiting_human`, and letting
   * the poll delete the card mid-press would pull the button out from under
   * the pointer before the answer arrives.
   *
   * The updater must be **pure**, and that is not a style note — it is the bug
   * this code shipped with. The `byId` map used to be built outside the
   * updater and drained with `byId.delete()` inside it. React invokes a state
   * updater more than once per transition in StrictMode (the cache-sync effect
   * above says so in as many words), and the second invocation found an
   * already-emptied map: every held run looked absent from the server list and
   * was dropped. Net effect in a development build, every time — `requests`
   * settled back to empty, so the takeover beacon and the takeover card were
   * unreachable and the handoff feature could not be seen at all.
   *
   * So the map is read-only and "already emitted" is tracked in a `Set` that is
   * created inside the updater. Same result, and running it twice produces the
   * same answer as running it once.
   */
  const applyServerList = useCallback(
    (incoming: TakeoverRequest[]) => {
      const fresh = incoming.filter((r) => !isRecentlyResolved(r.runId))
      const byId = new Map(fresh.map((r) => [r.runId, r] as const))
      setRequests((prev) => {
        const kept: TakeoverRequest[] = []
        const emitted = new Set<string>()
        for (const held of prev) {
          const update = byId.get(held.runId)
          if (update) {
            kept.push(mergeRequest(held, update))
            emitted.add(held.runId)
          } else if (inFlight.current.has(held.runId)) {
            kept.push(held)
          }
          // Otherwise it is dropped; the cache-sync effect above forgets it.
        }
        for (const arrival of fresh) if (!emitted.has(arrival.runId)) kept.push(arrival)
        return sortRequests(kept)
      })
    },
    [isRecentlyResolved],
  )

  const refresh = useCallback(async () => {
    if (!enabled) return
    try {
      const runs = await api.listAwaitingHumanRuns()
      if (!mounted.current) return
      applyServerList((runs ?? []).map((run) => applyRememberedCopy(requestFromRun(run))))
      setLoadError(null)
    } catch (err) {
      if (!mounted.current) return
      // A 404/422 here means this build is talking to an API that predates the
      // status. That is a missing feature, not a broken app: hold whatever the
      // live stream gave us and stay quiet.
      setLoadError(errorMessage(err))
    }
  }, [enabled, applyServerList])

  useEffect(() => {
    if (!enabled) return
    void refresh()
    const timer = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(timer)
  }, [enabled, refresh])

  const resume = useCallback(
    async (runId: string, note?: string): Promise<ResumeOutcome> => {
      // Synchronous, before any await. Two clicks in one frame: the second one
      // lands here and stops.
      if (inFlight.current.has(runId)) return { kind: "ignored" }
      inFlight.current.add(runId)
      setResuming((prev) => new Set(prev).add(runId))

      const trimmed = note?.trim()

      try {
        const result = await api.resumeRun(runId, trimmed ? { note: trimmed } : undefined)
        if (result?.resumed === false) {
          // Idempotent no-op. The agent is already going; the card's job here
          // is done either way.
          clear(runId)
          return { kind: "already-running", result }
        }
        clear(runId)
        return { kind: "resumed", result }
      } catch (err) {
        const code = err instanceof ApiError ? err.code : null
        const httpStatus = err instanceof ApiError ? err.status : 0
        // 404: not ours or already gone. 409 `run_not_resumable`: nothing to
        // continue from. Both mean the card is a dead end — say so and remove
        // it rather than leaving a button that can only fail again.
        const gone = httpStatus === 404 || code === "run_not_resumable"
        if (gone) clear(runId)
        return { kind: "failed", error: errorMessage(err), code, gone }
      } finally {
        inFlight.current.delete(runId)
        if (mounted.current) {
          setResuming((prev) => {
            const next = new Set(prev)
            next.delete(runId)
            return next
          })
          // Whatever happened, the parked list has moved on.
          void refresh()
        }
      }
    },
    [clear, refresh],
  )

  const dismiss = useCallback((runId: string) => {
    setDismissed((prev) => new Set(prev).add(runId))
  }, [])

  const undismiss = useCallback((runId: string) => {
    setDismissed((prev) => {
      if (!prev.has(runId)) return prev
      const next = new Set(prev)
      next.delete(runId)
      return next
    })
  }, [])

  const markPresented = useCallback((runId: string) => {
    setPresented((prev) => (prev.has(runId) ? prev : new Set(prev).add(runId)))
  }, [])

  const unpresented = useMemo(
    () => requests.find((r) => r.source === "live" && !presented.has(r.runId)) ?? null,
    [requests, presented],
  )

  const value = useMemo<TakeoverContextValue>(
    () => ({
      requests,
      dismissed,
      forBot: (botId) => (botId ? requests.filter((r) => r.botId === botId) : []),
      byRunId: (runId) => requests.find((r) => r.runId === runId) ?? null,
      unpresented,
      markPresented,
      raise: upsert,
      clear,
      dismiss,
      undismiss,
      resume,
      isResuming: (runId) => resuming.has(runId),
      refresh,
      loadError,
    }),
    [
      requests,
      dismissed,
      unpresented,
      markPresented,
      upsert,
      clear,
      dismiss,
      undismiss,
      resume,
      resuming,
      refresh,
      loadError,
    ],
  )

  return <TakeoverContext.Provider value={value}>{children}</TakeoverContext.Provider>
}

export function useTakeover(): TakeoverContextValue {
  const ctx = useContext(TakeoverContext)
  if (!ctx) throw new Error("useTakeover must be used inside <AppProviders>")
  return ctx
}
