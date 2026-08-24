/** Bot Desktop lifecycle, input forwarding and the screenshot fallback feed. */
import { useCallback, useEffect, useRef, useState } from "react"
import * as api from "../api/endpoints"
import { errorMessage, isAbortError } from "../api/client"
import { desktopStreamUrl } from "../lib/desktopStream"
import { useAsyncResource } from "./useAsync"
import type { BotDesktop, DesktopActionInput, DesktopActionOutcome, DesktopScreenshot } from "../types"

export type DesktopOp = "start" | "stop" | "suspend" | "resume" | null

export interface DesktopApi {
  desktop: BotDesktop | null
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  busy: DesktopOp
  /**
   * When this client last put the desktop into a transitional state, or null
   * when it is not in one.
   *
   * The server's `updated_at` cannot answer "how long have *I* been waiting":
   * a desktop already `starting` when the panel mounted has a timestamp from
   * before this window existed, and a fresh start has no server row until the
   * POST returns. This is the clock the boot progress is drawn against.
   */
  transitionSince: number | null
  start: () => Promise<void>
  stop: (wipe?: boolean) => Promise<void>
  suspend: () => Promise<void>
  resume: () => Promise<void>
  /** Resolves to a `PendingApprovalOut` when the action is risk-gated. */
  sendAction: (input: DesktopActionInput) => Promise<DesktopActionOutcome>
  actionError: string | null
  clearActionError: () => void
  lifecycleError: string | null
}

const TRANSIENT_STATES = new Set(["starting", "stopping"])

export function useDesktop(botId: string | null): DesktopApi {
  const [busy, setBusy] = useState<DesktopOp>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [lifecycleError, setLifecycleError] = useState<string | null>(null)
  const [transitionSince, setTransitionSince] = useState<number | null>(null)

  const resource = useAsyncResource<BotDesktop | null>(
    (signal) => (botId ? api.getDesktop(botId, signal) : Promise.resolve(null)),
    [botId],
    { initialData: null, enabled: Boolean(botId) },
  )
  const { data: desktop, setData, refetch } = resource

  // The row as it stands, for the rollback snapshot in `runOp`. A `setData`
  // updater is not a place to read a "before" value out of.
  const desktopRef = useRef<BotDesktop | null>(desktop)
  desktopRef.current = desktop

  // Poll while the container is coming up or going down.
  useEffect(() => {
    if (!botId || !desktop || !TRANSIENT_STATES.has(String(desktop.state))) return
    const timer = setInterval(() => void refetch(), 2500)
    return () => clearInterval(timer)
  }, [botId, desktop, refetch])

  /*
   * Two ways the local transition clock ends, and both matter.
   *
   * The desktop settling is the obvious one. The other is finding a desktop
   * *already* transitional on arrival — a boot this window did not start, from
   * a routine or another device. That also deserves a clock, and starting it
   * here rather than at the button means the progress reads honestly either
   * way: it counts from when this client first saw the transition, and says so.
   */
  const state = desktop?.state
  useEffect(() => {
    if (state && TRANSIENT_STATES.has(String(state))) {
      setTransitionSince((prev) => prev ?? Date.now())
    } else {
      setTransitionSince(null)
    }
  }, [state])

  // A different bot is a different machine; never carry a clock across.
  useEffect(() => setTransitionSince(null), [botId])

  /*
   * Lifecycle, applied to the UI before the server confirms it.
   *
   * `POST /desktop/start` does not return until the container group has been
   * asked for, and on a cold start that is not instant. The old behaviour was a
   * disabled button and no other change until it came back, so pressing Start
   * on a bot whose desktop takes ninety seconds to appear looked, for the first
   * several of them, exactly like pressing a dead button.
   *
   * `optimisticState` is the state the operation is *known* to be entering —
   * `starting`, `stopping` — never the state it hopes to reach. `starting` is
   * true the moment the request leaves; `running` would be a guess. If the call
   * fails the previous row is put straight back, so the pane never keeps a
   * transition that did not happen.
   */
  const runOp = useCallback(
    async (op: Exclude<DesktopOp, null>, fn: () => Promise<BotDesktop>, optimisticState?: BotDesktop["state"]) => {
      setBusy(op)
      setLifecycleError(null)
      const previous = desktopRef.current
      if (optimisticState) {
        setTransitionSince(Date.now())
        setData((prev) => ({ ...(prev ?? { bot_id: botId ?? "" }), state: optimisticState }))
      }
      try {
        const next = await fn()
        setData(next)
        // A server row that is no longer transitional ends the local clock; a
        // `starting` row keeps it, because the wait is not over.
        if (!TRANSIENT_STATES.has(String(next.state))) setTransitionSince(null)
      } catch (err) {
        if (optimisticState) {
          setData(previous)
          setTransitionSince(null)
        }
        setLifecycleError(errorMessage(err))
        throw err
      } finally {
        setBusy(null)
      }
    },
    [botId, setData],
  )

  const start = useCallback(async () => {
    if (!botId) return
    await runOp("start", () => api.startDesktop(botId), "starting")
  }, [botId, runOp])

  const stop = useCallback(
    async (wipe = false) => {
      if (!botId) return
      await runOp("stop", () => api.stopDesktop(botId, wipe), "stopping")
    },
    [botId, runOp],
  )

  const suspend = useCallback(async () => {
    if (!botId) return
    await runOp("suspend", () => api.suspendDesktop(botId))
  }, [botId, runOp])

  const resume = useCallback(async () => {
    if (!botId) return
    await runOp("resume", () => api.resumeDesktop(botId))
  }, [botId, runOp])

  const sendAction = useCallback(
    async (input: DesktopActionInput) => {
      if (!botId) throw new Error("No bot selected")
      setActionError(null)
      try {
        return await api.desktopAction(botId, input)
      } catch (err) {
        setActionError(errorMessage(err))
        throw err
      }
    },
    [botId],
  )

  return {
    desktop,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch,
    busy,
    transitionSince,
    start,
    stop,
    suspend,
    resume,
    sendAction,
    actionError,
    clearActionError: useCallback(() => setActionError(null), []),
    lifecycleError,
  }
}

export interface DesktopStreamView {
  /** `src` for the viewer iframe, or null until a ticket has been minted. */
  url: string | null
  loading: boolean
  error: string | null
  /** Mint a fresh ticket — the previous one is single-use and short-lived. */
  refresh: () => void
}

/**
 * The live noVNC view, via the API's stream proxy.
 *
 * The desktop itself has no public address, so the viewer is served through the
 * API and authenticated with a short-lived ticket rather than a bearer token —
 * an `<iframe src>` cannot send a header. Minting is therefore a real API call
 * that has to happen before the iframe can be pointed anywhere, which is why
 * this is a hook and not a derived string.
 *
 * The ticket is spent by the WebSocket noVNC opens, so there is exactly one
 * viewing session per mint: reconnecting means calling `refresh`.
 */
export function useDesktopStream(botId: string | null, options: { enabled?: boolean } = {}): DesktopStreamView {
  const { enabled = false } = options
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    if (!botId || !enabled) return

    let cancelled = false
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    void (async () => {
      try {
        const ticket = await api.createDesktopStreamTicket(botId, controller.signal)
        if (cancelled) return
        setUrl(desktopStreamUrl(ticket))
      } catch (err) {
        if (cancelled || isAbortError(err)) return
        setUrl(null)
        setError(errorMessage(err))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [botId, enabled, nonce])

  // A ticket belongs to one bot; never let the previous bot's URL survive.
  useEffect(() => {
    setUrl(null)
    setError(null)
  }, [botId])

  return {
    url,
    loading,
    error,
    refresh: useCallback(() => setNonce((n) => n + 1), []),
  }
}

export interface ScreenshotFeed {
  frame: DesktopScreenshot | null
  error: string | null
  /** Timestamp of the last successful frame. */
  updatedAt: number | null
  stalled: boolean
  /**
   * Pull a frame now instead of waiting out the interval. Called after every
   * forwarded click and keystroke: in takeover the delay between doing a thing
   * and seeing it is the whole experience, and a fixed poll makes even a fast
   * desktop feel broken. Coalesces with an in-flight request, so it can be
   * called as often as the user can type.
   */
  refresh: () => void
}

/**
 * Poll `GET /bots/{id}/desktop/screenshot`. Sequential (never overlapping) so a
 * slow sidecar cannot pile up requests.
 */
export function useDesktopScreenshot(
  botId: string | null,
  options: { enabled?: boolean; intervalMs?: number } = {},
): ScreenshotFeed {
  const { enabled = false, intervalMs = 1000 } = options
  const [frame, setFrame] = useState<DesktopScreenshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const failuresRef = useRef(0)
  // Set by the live effect; cleared when it tears down. A `refresh()` from a
  // component that has already unmounted must be a no-op, not a stray fetch.
  const kickRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    if (!botId || !enabled) return

    let cancelled = false
    let inFlight = false
    let kicked = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const controller = new AbortController()

    const tick = async () => {
      if (cancelled) return
      // One request at a time, always. A kick that lands mid-flight is
      // remembered and turns into an immediate re-poll on completion.
      if (inFlight) {
        kicked = true
        return
      }
      inFlight = true
      try {
        const shot = await api.desktopScreenshot(botId, controller.signal)
        if (cancelled) return
        failuresRef.current = 0
        if (shot && shot.png_base64) {
          setFrame(shot)
          setUpdatedAt(Date.now())
          setError(shot.ok === false ? (shot.error ?? "Screenshot unavailable") : null)
        } else {
          setError(shot?.error ?? "Screenshot unavailable")
        }
      } catch (err) {
        if (cancelled || isAbortError(err)) return
        failuresRef.current += 1
        setError(errorMessage(err))
      } finally {
        inFlight = false
        if (!cancelled) {
          // Back off when the sidecar is unhappy so we do not hammer it.
          const backoff = Math.min(failuresRef.current, 5) * 1000
          const wait = kicked && failuresRef.current === 0 ? 0 : intervalMs + backoff
          kicked = false
          if (timer) clearTimeout(timer)
          timer = setTimeout(() => void tick(), wait)
        }
      }
    }

    kickRef.current = () => {
      if (cancelled) return
      if (inFlight) {
        kicked = true
        return
      }
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => void tick(), 0)
    }

    void tick()

    return () => {
      cancelled = true
      kickRef.current = null
      controller.abort()
      if (timer) clearTimeout(timer)
    }
  }, [botId, enabled, intervalMs])

  useEffect(() => {
    if (!enabled) {
      setFrame(null)
      setUpdatedAt(null)
      setError(null)
    }
  }, [enabled])

  return {
    frame,
    error,
    updatedAt,
    stalled: updatedAt !== null && Date.now() - updatedAt > 5000,
    refresh: useCallback(() => kickRef.current?.(), []),
  }
}
