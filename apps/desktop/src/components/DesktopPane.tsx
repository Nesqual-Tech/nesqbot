import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react"
import { botStateLabels, type BotStateToken } from "@nesqbot/ui"
import { errorMessage } from "../api/client"
import { isPendingApproval } from "../api/endpoints"
import { useDesktop, useDesktopScreenshot, useDesktopStream } from "../hooks/useDesktop"
import type { DesktopLayoutApi } from "../hooks/useDesktopLayout"
import {
  DEFAULT_DESKTOP_SIZE,
  desktopToPercent,
  fitSize,
  normaliseSize,
  pointToDesktop,
  scalePercent,
  type Point,
  type Size,
} from "../lib/desktopGeometry"
import { cx, relativeTime } from "../lib/format"
import { dur, ease, gsap, prefersReducedMotion, useGSAP } from "../lib/motion"
import { useRecorder, useToast } from "../state/AppState"
import { useTakeover } from "../state/takeover"
import { DesktopBoot } from "./DesktopBoot"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { RoutineRecorder } from "./RoutineRecorder"
import { Spinner } from "./Spinner"
import { TakeoverCard } from "./TakeoverCard"
import type { Bot, DesktopActionInput } from "../types"

export interface DesktopPaneProps {
  bot: Bot | null
  layout: DesktopLayoutApi
  /**
   * Closes the pane. Present because the pane is no longer a permanent
   * column: it is opened from a conversation, so it has to be closable from
   * where it is rather than only from the button that opened it.
   */
  onClose?: () => void
}

type ViewMode = "stream" | "canvas"

const IFRAME_TIMEOUT_MS = 8000

/**
 * The keys the pane can press on the bot's machine.
 *
 * X11 keysym names, which is what `POST /desktop/action` expects. Escape is
 * here and the pane's own Escape is not the same key: pressing Esc while the
 * canvas has focus releases control back to the bot, so the only way to send a
 * real Escape *to* the desktop is a button. That is exactly the sort of thing
 * a `title` has to say out loud.
 */
const KEY_SENDS: Array<{ label: string; code: string; title: string }> = [
  { label: "Enter", code: "Return", title: "Press Enter on the bot desktop" },
  { label: "Tab", code: "Tab", title: "Press Tab on the bot desktop — moves between form fields" },
  { label: "Esc", code: "Escape", title: "Press Escape on the bot desktop. The pane's own Esc releases control instead." },
]

const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead"])

/**
 * Screenshot cadence. Watching a bot work does not need to be fast; driving a
 * login form does, because every keystroke you cannot see the result of is a
 * keystroke you type twice. Each frame is also forced immediately after an
 * action, so the poll interval is a floor on staleness, not on feedback.
 */
const POLL_WATCHING_MS = 1200
const POLL_TAKEOVER_MS = 600

/** How long the "you clicked here" marker stays readable under reduced motion. */
const MARKER_STATIC_S = 0.45

export function DesktopPane({ bot, layout, onClose }: DesktopPaneProps) {
  const toast = useToast()
  const recorder = useRecorder()
  const handoff = useTakeover()
  const desktop = useDesktop(bot?.id ?? null)

  const [mode, setMode] = useState<ViewMode>("stream")
  const [iframeFailed, setIframeFailed] = useState(false)
  const [iframeLoaded, setIframeLoaded] = useState(false)
  const [takeover, setTakeover] = useState(false)
  const [canvasFocused, setCanvasFocused] = useState(false)
  const [typeBuffer, setTypeBuffer] = useState("")
  const [confirmWipe, setConfirmWipe] = useState(false)

  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const paneRef = useRef<HTMLElement | null>(null)
  const markerRef = useRef<HTMLSpanElement | null>(null)
  const markerTl = useRef<gsap.core.Timeline | null>(null)
  const state = String(desktop.desktop?.state ?? "absent")

  /*
   * The handoff, if this bot has one.
   *
   * "Not now" hides the card but never the run — `TakeoverBeacon` in the shell
   * keeps it reachable, and clicking that puts it back. A run must never become
   * unfindable because somebody pressed the wrong button once.
   */
  const requestsForBot = handoff.forBot(bot?.id)
  const request = requestsForBot.find((item) => !handoff.dismissed.has(item.runId)) ?? null
  const requestRunId = request?.runId ?? null

  const { expanded, zoom } = layout

  // The desktop's own `stream_url` is a private VNet address (`http://10.60.4.x:6901`)
  // and is unreachable from here on purpose — pointing the iframe at it is what
  // produced "This content is blocked." The viewable URL comes from the API's
  // stream proxy instead, and needs a minted ticket, so it is asked for only
  // while the live view is actually wanted.
  const wantStream = mode === "stream" && state === "running" && !iframeFailed
  const stream = useDesktopStream(bot?.id ?? null, { enabled: wantStream })
  const streamUrl = wantStream ? stream.url : null

  const canvasMode = !wantStream || (stream.error !== null && stream.url === null)
  const feedEnabled = canvasMode && (state === "running" || state === "suspended")
  const shots = useDesktopScreenshot(bot?.id ?? null, {
    enabled: feedEnabled,
    intervalMs: takeover ? POLL_TAKEOVER_MS : POLL_WATCHING_MS,
  })

  /* ------------------------------------------------------------------ *
   * Sizing
   *
   * The stage is sized in JS rather than by CSS because `aspect-ratio` cannot
   * express "contain" — with a fixed width it lets `max-height` squash the
   * ratio, and with a fixed height `max-width` squashes it the other way. So a
   * ResizeObserver measures the viewport and `fitSize` produces the largest
   * whole-pixel box with the desktop's proportions that fits inside it.
   *
   * The canvas also keeps `object-fit: contain`, which is a no-op while the
   * element matches the desktop's ratio and a safety net for the frame after a
   * resize where it does not — and `pointToDesktop` maps through exactly the
   * same contain geometry, so the two can never disagree about where a pixel is.
   * ------------------------------------------------------------------ */

  const desktopSize = useMemo<Size>(() => normaliseSize(shots.frame, DEFAULT_DESKTOP_SIZE), [shots.frame])

  const [viewportSize, setViewportSize] = useState<Size>({ width: 0, height: 0 })

  useLayoutEffect(() => {
    const node = viewportRef.current
    if (!node) return
    const measure = () => {
      const rect = node.getBoundingClientRect()
      setViewportSize((prev) =>
        Math.abs(prev.width - rect.width) < 0.5 && Math.abs(prev.height - rect.height) < 0.5
          ? prev
          : { width: rect.width, height: rect.height },
      )
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [expanded, state, canvasMode])

  const stageSize = useMemo<Size>(() => {
    if (zoom === "actual") return desktopSize
    if (viewportSize.width < 1 || viewportSize.height < 1) return desktopSize
    return fitSize(viewportSize, desktopSize)
  }, [zoom, viewportSize, desktopSize])

  const stageStyle = useMemo<CSSProperties>(
    () => ({ width: `${stageSize.width}px`, height: `${stageSize.height}px` }),
    [stageSize],
  )

  const scale = scalePercent(stageSize, desktopSize)

  // Reset the view whenever the bot changes, or the desktop's lifecycle moves —
  // a desktop that just came up deserves a fresh attempt at the live stream.
  // Deliberately *not* keyed on the stream URL: minting a ticket changes it, and
  // clearing `iframeFailed` there would re-enable the hook and mint again.
  useEffect(() => {
    setIframeFailed(false)
    setIframeLoaded(false)
    setTakeover(false)
    setMode("stream")
  }, [bot?.id, state])

  // An iframe that never loads (CSP, dead proxy, expired ticket) is a failure.
  useEffect(() => {
    if (!streamUrl || iframeLoaded || iframeFailed || mode !== "stream") return
    const timer = setTimeout(() => setIframeFailed(true), IFRAME_TIMEOUT_MS)
    return () => clearTimeout(timer)
  }, [streamUrl, iframeLoaded, iframeFailed, mode])

  // Paint the latest screenshot. The backing store is the frame's own size, not
  // the element's and not `devicePixelRatio` — see `lib/desktopGeometry`.
  useEffect(() => {
    const frame = shots.frame
    const canvas = canvasRef.current
    if (!frame?.png_base64 || !canvas) return
    const context = canvas.getContext("2d")
    if (!context) return

    const image = new Image()
    let cancelled = false
    image.onload = () => {
      if (cancelled) return
      if (canvas.width !== image.naturalWidth) canvas.width = image.naturalWidth
      if (canvas.height !== image.naturalHeight) canvas.height = image.naturalHeight
      context.drawImage(image, 0, 0)
    }
    image.src = frame.png_base64.startsWith("data:") ? frame.png_base64 : `data:image/png;base64,${frame.png_base64}`

    return () => {
      cancelled = true
      image.onload = null
    }
  }, [shots.frame])

  const shotsRefresh = shots.refresh
  const perform = useCallback(
    async (input: DesktopActionInput) => {
      if (!bot) return
      /*
       * `record` no-ops unless recording is armed for this exact bot — and it
       * is suppressed outright while a takeover is outstanding.
       *
       * A takeover exists because the agent hit a login. Recording turns what
       * the operator does into a saved routine, and a routine step reading
       * `type "hunter2"` is a password written to the database in plain text.
       * The whole promise of this pane is that the credential goes to the
       * bot's browser and nowhere else, so the recorder pauses rather than
       * being trusted to be off.
       */
      if (recorder.botId === bot.id && !request) recorder.record(input)
      try {
        const result = await desktop.sendAction(input)
        if (isPendingApproval(result)) {
          // 201 PendingApprovalOut — the action was held, not executed.
          toast.warning(
            result.title || "Approval required",
            result.detail || `${input.action} was held for review (risk: ${result.risk}).`,
          )
        } else if (result?.ok === false && result.error) {
          toast.error("Desktop action failed", result.error)
        }
      } catch (err) {
        toast.error("Desktop action failed", errorMessage(err))
      } finally {
        // Do not wait a whole poll interval to see what the action did.
        shotsRefresh()
      }
    },
    [bot, desktop, recorder, request, toast, shotsRefresh],
  )

  /* ------------------------------------------------------------------ *
   * Pointer → desktop pixel
   * ------------------------------------------------------------------ */

  const toCoords = useCallback(
    (event: ReactMouseEvent<HTMLCanvasElement>): Point | null => {
      const canvas = canvasRef.current
      if (!canvas) return null
      const rect = canvas.getBoundingClientRect()
      // `desktopSize` is the frame currently painted, which is the only thing a
      // click can meaningfully be expressed in.
      return pointToDesktop(event.clientX, event.clientY, rect, desktopSize)
    },
    [desktopSize],
  )

  /* ------------------------------------------------------------------ *
   * "You clicked here"
   *
   * The marker element is mounted permanently and parked invisible, rather
   * than being a piece of React state that mounts and unmounts on every click.
   * Two reasons, and neither is the animation:
   *
   *  - A click used to cost a `setState` plus a `setTimeout` plus a second
   *    `setState`, i.e. two renders of this whole pane per click. Driving a
   *    login form is a lot of clicks.
   *  - It is an absolutely positioned, `pointer-events: none` sibling *inside*
   *    the stage. It never touches the canvas element or the stage box, so it
   *    cannot perturb the click mapping the pane is built around. Nothing in
   *    this file animates a transform on either of those two elements, and
   *    that is deliberate: a transform on an ancestor of the canvas is the one
   *    change that could silently un-verify the 0px mapping.
   *
   * Created inside a pointer handler, so it goes through `contextSafe` — a
   * tween born in an event callback is outside the hook's context and would
   * otherwise survive unmount.
   * ------------------------------------------------------------------ */

  const { contextSafe } = useGSAP({ scope: paneRef })

  const flashMarker = contextSafe((point: Point) => {
    const node = markerRef.current
    if (!node) return

    // A second click while the first ring is still going replaces it. Without
    // this, fast clicking stacks timelines that fight over the same element.
    markerTl.current?.kill()

    const position = desktopToPercent(point, desktopSize)
    gsap.set(node, { left: position.left, top: position.top })

    const ring = node.querySelector(".desktop-marker__ring")
    const dot = node.querySelector(".desktop-marker__dot")
    const tl = gsap.timeline()

    if (prefersReducedMotion()) {
      // The marker is *feedback*, not decoration: somebody driving a sign-in
      // needs to see where their click landed. So reduced motion loses the
      // expansion and keeps the mark, held still and then cleared.
      tl.set([ring, dot], { autoAlpha: 1, scale: 1 }).set([ring, dot], { autoAlpha: 0 }, MARKER_STATIC_S)
    } else {
      tl.fromTo(
        ring,
        { scale: 0.35, autoAlpha: 1 },
        { scale: 1.9, autoAlpha: 0, duration: dur("deliberate"), ease: ease("exit") },
        0,
      ).fromTo(
        dot,
        { scale: 0.5, autoAlpha: 1 },
        { scale: 1, autoAlpha: 0, duration: dur("slow"), ease: ease("exit") },
        0,
      )
    }

    markerTl.current = tl
  })

  const onCanvasClick = (event: ReactMouseEvent<HTMLCanvasElement>) => {
    if (!takeover) return
    const point = toCoords(event)
    if (!point) return
    flashMarker(point)
    void perform({ action: event.detail >= 2 ? "double_click" : "click", button: "left", ...point })
  }

  const onCanvasContextMenu = (event: ReactMouseEvent<HTMLCanvasElement>) => {
    if (!takeover) return
    event.preventDefault()
    const point = toCoords(event)
    if (!point) return
    flashMarker(point)
    void perform({ action: "right_click", button: "right", ...point })
  }

  /* ------------------------------------------------------------------ *
   * Takeover and the keyboard
   * ------------------------------------------------------------------ */

  /*
   * Derived, never stored. Storing it meant the flag was written by whichever
   * of "takeover turned on" and "the canvas took focus" happened to land
   * second, and the focus call is one frame after the state change — so the
   * focus handler could read a stale `takeover === false` and leave the bar
   * claiming the keyboard was not live while it was. Deriving it makes the
   * ordering irrelevant.
   */
  const keyboardLive = takeover && canvasFocused

  const releaseKeyboard = useCallback(() => {
    canvasRef.current?.blur()
  }, [])

  /*
   * Whether the pending activation should also claim the keyboard.
   *
   * A person pressing "Take over" is asking for the keyboard and gets it. A
   * *takeover request* arriving from the agent is not the same act: it can land
   * at any moment, including mid-sentence in the composer, and silently
   * redirecting the next keystroke to a remote browser because a background run
   * hit a login is the one thing a takeover UI must never do. So the arriving
   * request arms the pane — canvas mode, click forwarding, the amber bar — and
   * leaves the keyboard where it is until the person clicks the screen. The bar
   * and the card both say exactly that in the meantime.
   */
  const focusOnActivate = useRef(true)

  const setTakeoverMode = useCallback(
    (next: boolean, announce = true, claimKeyboard = true) => {
      focusOnActivate.current = claimKeyboard
      setTakeover(next)
      // Focusing is an effect keyed on `takeover`, not a call from here: when
      // the pane is on the live stream the canvas does not exist yet at this
      // point, and it has to be focused after it mounts.
      if (next) {
        if (!canvasMode) setMode("canvas")
      } else {
        releaseKeyboard()
      }
      if (announce) toast.info(next ? "You have control" : "Control handed back to the bot")
    },
    [canvasMode, releaseKeyboard, toast],
  )

  const onCanvasKeyDown = (event: ReactKeyboardEvent<HTMLCanvasElement>) => {
    if (!takeover) return

    // The escape hatch, and the only key the desktop never sees. Everything
    // else — including Tab, which a sign-in form needs — is forwarded, so this
    // has to be reliable and it has to be advertised (it is, in the input bar).
    // `stopPropagation` keeps the window-level handler from also collapsing the
    // maximised view: one Esc releases the keyboard, a second leaves fullscreen.
    if (event.key === "Escape") {
      event.preventDefault()
      event.stopPropagation()
      setTakeoverMode(false)
      return
    }

    if (MODIFIER_KEYS.has(event.key)) return
    event.preventDefault()

    const combo: string[] = []
    if (event.ctrlKey) combo.push("ctrl")
    if (event.altKey) combo.push("alt")
    if (event.metaKey) combo.push("meta")
    if (event.shiftKey && event.key.length > 1) combo.push("shift")

    if (combo.length === 0 && event.key.length === 1) {
      void perform({ action: "type", text: event.key })
      return
    }

    // `event.key` verbatim, NOT lowercased. The sidecar translates browser key
    // names to X11 keysyms, and that table is keyed case-insensitively, but a
    // key it does not know — `KP_Enter`, a keysym someone passes deliberately —
    // survives only if we do not mangle it on the way out.
    combo.push(event.key)

    // A chord is one keystroke with modifiers held, which is `key_combo`.
    // `key` sends its list *sequentially*: `{keys:["ctrl","a"]}` pressed Ctrl,
    // released it, then pressed A — so every shortcut a person tried during a
    // takeover did the wrong thing quietly.
    if (combo.length > 1) {
      void perform({ action: "key_combo", keys: combo })
      return
    }
    void perform({ action: "key", keys: combo })
  }

  // Esc anywhere else in the pane leaves the maximised view. Bubble phase, so a
  // focused canvas gets first refusal via `stopPropagation` above.
  useEffect(() => {
    if (!expanded) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return
      layout.setExpanded(false)
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [expanded, layout])

  // Taking over should put the keyboard where the button just said it goes,
  // including when the switch from the live stream mounts the canvas a commit
  // later.
  useEffect(() => {
    if (!takeover || !canvasMode || !focusOnActivate.current) return
    const node = canvasRef.current
    if (node && document.activeElement !== node) node.focus()
  }, [takeover, canvasMode])

  /*
   * A takeover request arms the pane, and answering it disarms it again.
   *
   * Arming means: switch to the screenshot canvas, start forwarding clicks,
   * raise the amber bar — everything except taking the keyboard, which stays
   * with whatever the person was typing into until they click the screen.
   *
   * The key carries the lifecycle state as well as the run, because the reset
   * effect above clears `takeover` on every state change; without that the pane
   * would go quietly read-only the moment the desktop finished starting, with
   * the card still saying "sign in below".
   */
  const armed = useRef<{ key: string; runId: string } | null>(null)
  useEffect(() => {
    if (requestRunId && (state === "running" || state === "suspended")) {
      const key = `${requestRunId}:${state}`
      if (armed.current?.key !== key) {
        armed.current = { key, runId: requestRunId }
        setTakeoverMode(true, false, false)
      }
      return
    }
    // The request is gone — resumed, finished, or cancelled. If this pane put
    // itself in takeover for it, hand the machine back: the agent is driving
    // again and a stray click would land in the middle of its work.
    if (armed.current && !requestRunId) {
      armed.current = null
      setTakeoverMode(false, false)
    }
  }, [requestRunId, state, setTakeoverMode])

  const sendTypeBuffer = () => {
    const text = typeBuffer
    if (!text) return
    setTypeBuffer("")
    void perform({ action: "type", text })
  }

  const busyLabel = desktop.busy ? `${desktop.busy}…` : null
  const canStart = state === "absent" || state === "error" || state === "stopped"
  const canStop = state !== "absent" && state !== "stopping"
  const canSuspend = state === "running"
  const canResume = state === "suspended"

  const lifecycleProblem = desktop.lifecycleError ?? desktop.desktop?.last_error ?? null

  const stateTone = useMemo(() => {
    if (state === "running") return "ok"
    if (state === "error") return "error"
    if (state === "starting" || state === "stopping" || state === "suspended") return "warn"
    return "idle"
  }, [state])

  /* ------------------------------------------------------------------ *
   * Mode changes
   *
   * Docked and maximised are two different shapes of the same pane, and the
   * grid track swaps instantly. What animates is the *chrome* re-forming
   * around the new size: header, lifecycle controls, input bar and footer
   * settle in, so the change reads as deliberate rather than as a jump cut.
   *
   * What deliberately does not animate is `.desktop-viewport`, `.desktop-stage`
   * and the canvas. Their geometry is what `pointToDesktop` maps through, and
   * the pane's 0px click accuracy is measured, not assumed. Opacity and
   * transform on sibling chrome cause no reflow, so the viewport's measured
   * box is identical whether this timeline is running or not.
   * ------------------------------------------------------------------ */
  useGSAP(
    () => {
      gsap.from(".pane__header, .desktop-controls, .desktop-inputbar, .desktop-footer", {
        y: expanded ? 6 : -6,
        autoAlpha: 0,
        duration: dur("base"),
        ease: ease("entrance"),
        stagger: prefersReducedMotion() ? 0 : 0.03,
      })
    },
    { dependencies: [expanded], scope: paneRef, revertOnUpdate: true },
  )

  /*
   * The second half of the handoff.
   *
   * `.desktop-pane--takeover` is the persistent state: a static ring saying
   * "this surface is live". This is the *event*: one ring closing onto the
   * pane at the moment control changes hands. State and event are different
   * things and get different treatment, which is why the ring pulse is not
   * simply the static ring fading in.
   */
  useGSAP(
    () => {
      if (!takeover || prefersReducedMotion()) return
      gsap.fromTo(
        ".desktop-pane__arm",
        { autoAlpha: 1, scale: 1.015 },
        { autoAlpha: 0, scale: 1, duration: dur("deliberate"), ease: ease("exit") },
      )
    },
    { dependencies: [takeover], scope: paneRef, revertOnUpdate: true },
  )

  if (!bot) {
    /*
     * Still wears `--expanded` when the preference says so. The shell collapses
     * the pane's grid track the moment the class is on the app, so an empty
     * pane that quietly dropped it went from "out of flow, covering the window"
     * to "in flow, with no column to sit in" and wrapped under the sidebar.
     */
    return (
      <section
        ref={paneRef}
        className={cx("desktop-pane", expanded && "desktop-pane--expanded")}
        aria-label="Agent Computer"
      >
        <div className="pane__header">
          <h2 className="pane__title">Agent Computer</h2>
          {expanded ? (
            <button
              type="button"
              className="btn btn--ghost btn--icon"
              onClick={layout.toggleExpanded}
              title="Restore the pane (Esc)"
              aria-label="Restore the Agent Computer pane"
            >
              <Icon name="collapse" size={16} />
            </button>
          ) : null}
        </div>
        <EmptyState
          glyph="monitor"
          title="No teammate selected"
          description="Pick a bot to spin up its Linux desktop, watch it work, or take over."
        />
      </section>
    )
  }

  /*
   * The stage is for a machine that exists.
   *
   * This used to be `state !== "absent"`, which sent `starting` down the canvas
   * path — where the screenshot feed is deliberately switched off until the
   * desktop is `running`, so it rendered a spinner that could not resolve for
   * as long as the boot took. `error` went the same way and showed an empty
   * canvas over the top of its own error message. Both now go somewhere that
   * says something.
   */
  const transitioning = state === "starting" || state === "stopping"
  const showStage = state === "running" || state === "suspended"

  return (
    <section
      ref={paneRef}
      className={cx(
        "desktop-pane",
        expanded && "desktop-pane--expanded",
        takeover && "desktop-pane--takeover",
        keyboardLive && "desktop-pane--keys",
      )}
      aria-label={`Agent Computer for ${bot.name}`}
    >
      {/* The handoff ring. Purely an event marker; parked invisible. */}
      <span className="desktop-pane__arm" aria-hidden="true" />

      <div className="pane__header">
        <div>
          <h2 className="pane__title">{bot.name}&rsquo;s Computer</h2>
          <div className="pane__subtitle">
            {/*
              `data-state` carries the real lifecycle value so the pill can wear
              the design system's bot-state role: the dot is the fill (3:1, a
              non-text element) and the label is that colour re-derived for text
              (4.5:1). The tone class stays as the fallback for states the role
              table does not name.
            */}
            <span className={cx("state-pill", `state-pill--${stateTone}`)} data-state={state}>
              {botStateLabels[state as BotStateToken] ?? state}
            </span>
            {busyLabel ? <Spinner inline label={busyLabel} /> : null}
            {desktop.desktop?.container_id ? (
              <span className="pane__meta">{desktop.desktop.container_id.slice(0, 12)}</span>
            ) : null}
          </div>
        </div>

        <div className="desktop-pane__tools">
          <div className="segmented" role="group" aria-label="Desktop scale">
            <button
              type="button"
              className={cx("segmented__option", zoom === "fit" && "segmented__option--on")}
              aria-pressed={zoom === "fit"}
              onClick={() => layout.setZoom("fit")}
              title="Scale the desktop to fit the pane"
            >
              Fit
            </button>
            <button
              type="button"
              className={cx("segmented__option", zoom === "actual" && "segmented__option--on")}
              aria-pressed={zoom === "actual"}
              onClick={() => layout.setZoom("actual")}
              title="Show the desktop at 1:1 and scroll"
            >
              1:1
            </button>
          </div>
          <button
            type="button"
            className={cx("btn", "btn--ghost", "btn--icon", expanded && "btn--on")}
            onClick={layout.toggleExpanded}
            aria-pressed={expanded}
            title={expanded ? "Restore the pane (Esc)" : "Maximise the desktop"}
            aria-label={expanded ? "Restore the Agent Computer pane" : "Maximise the Agent Computer"}
          >
            <Icon name={expanded ? "collapse" : "expand"} size={16} />
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--icon"
            onClick={() => void desktop.refetch()}
            aria-label="Refresh desktop state"
            title="Refresh desktop state"
          >
            <Icon name="refresh" size={16} />
          </button>
          {onClose ? (
            <button
              type="button"
              className="btn btn--ghost btn--icon"
              onClick={onClose}
              aria-label="Close the Agent Computer"
              title="Close (Ctrl ⇧ D)"
            >
              <Icon name="close" size={16} />
            </button>
          ) : null}
        </div>
      </div>

      {/*
        The handoff, directly above the screen it is talking about. Not a toast
        and not a tab: "sign in on that machine" only makes sense next to the
        machine.
      */}
      {request ? (
        <TakeoverCard
          request={request}
          botName={bot.name}
          desktopState={state}
          onStartDesktop={() => void desktop.start().catch(() => undefined)}
          startingDesktop={desktop.busy === "start" || state === "starting"}
          keyboardLive={keyboardLive}
          hasControl={takeover}
          onTakeControl={() => setTakeoverMode(true, false, true)}
        />
      ) : null}

      {/*
        Container lifecycle.

        ## What was wrong with the old row

        Five buttons of near-equal weight, always all five, always all visible:
        Start, Suspend, Resume, Stop, and a filled-red `Stop + wipe`. Three of
        them were greyed out at any given moment because the lifecycle only
        ever permits two, and the single most destructive action in the
        product — irreversibly wiping a bot's home directory — was the most
        saturated pixel on a rail that is on screen on *every* tab. The
        interface was shouting its worst idea at you all day.

        ## What it is now

        One action is primary and it is the one the current state actually
        wants. Actions that cannot run are not rendered at all rather than
        rendered dead; the state pill directly above already says why. And
        `Stop + wipe` is an ordinary ghost button at rest and states its own
        consequence inline when pressed — danger belongs in the *word* and the
        confirmation, not in a permanent block of colour competing with the live
        screen underneath it.

        It disappears entirely while a run is parked on a human. Someone being
        asked to clear an MFA prompt is not there to manage containers, and
        "Stop + wipe" one row under "sign in, then press Continue" is an
        invitation to destroy the session they were asked to rescue.
      */}
      {request ? null : (
        <div className="desktop-controls" role="group" aria-label="Desktop lifecycle">
          {canStart ? (
            <button
              type="button"
              className="btn btn--primary btn--sm"
              disabled={desktop.busy !== null}
              onClick={() => void desktop.start().catch(() => undefined)}
            >
              Start desktop
            </button>
          ) : null}

          {canResume ? (
            <button
              type="button"
              className="btn btn--primary btn--sm"
              disabled={desktop.busy !== null}
              onClick={() => void desktop.resume().catch(() => undefined)}
            >
              Resume
            </button>
          ) : null}

          {canSuspend ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              disabled={desktop.busy !== null}
              onClick={() => void desktop.suspend().catch(() => undefined)}
            >
              Suspend
            </button>
          ) : null}

          {canStop ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              disabled={desktop.busy !== null}
              onClick={() => void desktop.stop(false).catch(() => undefined)}
            >
              Stop
            </button>
          ) : null}

          {/*
            The confirmation, in the pane, rather than a native `confirm()`.

            `window.confirm` in a packaged desktop app is a browser artefact: it
            is unstyled, it says "localhost says", it blocks the whole window,
            and it asks the question somewhere other than next to the machine it
            is about to erase. This product’s entire argument is that a
            consequential action stops and states its consequence in place, so
            its own most destructive button should behave the way the approval
            card does — the sentence, then the word, then two presses.
          */}
          {canStop ? (
            confirmWipe ? (
              <span className="danger-confirm" role="alert">
                <span className="danger-confirm__text">
                  Erases {bot.name}&rsquo;s home directory. Permanent — there is no undo.
                </span>
                <button
                  type="button"
                  className="btn btn--quiet-danger btn--sm"
                  disabled={desktop.busy !== null}
                  onClick={() => {
                    setConfirmWipe(false)
                    void desktop.stop(true).catch(() => undefined)
                  }}
                >
                  Erase and stop
                </button>
                <button type="button" className="btn btn--ghost btn--sm" autoFocus onClick={() => setConfirmWipe(false)}>
                  Cancel
                </button>
              </span>
            ) : (
              <button
                type="button"
                className="btn btn--ghost btn--sm desktop-controls__wipe"
                disabled={desktop.busy !== null}
                onClick={() => setConfirmWipe(true)}
                title="Stop the desktop and erase the bot home directory. This cannot be undone."
              >
                Stop + wipe
              </button>
            )
          ) : null}
        </div>
      )}

      {lifecycleProblem ? (
        <div className="inline-error" role="alert">
          <strong>Desktop error</strong>
          <pre>{lifecycleProblem}</pre>
        </div>
      ) : null}

      {desktop.error && !desktop.desktop ? (
        <ErrorState
          error={desktop.error}
          title="Desktop state unavailable"
          onRetry={() => void desktop.refetch()}
          compact
        />
      ) : null}

      <div
        className={cx("desktop-viewport", zoom === "actual" && "desktop-viewport--scroll")}
        ref={viewportRef}
        /*
         * The docked pane's height follows the desktop's own proportions, so
         * the common case has no letterbox at all. `max-height` can still
         * override that on a short window — which is exactly the case the
         * contain maths in `desktopGeometry` exists to absorb.
         */
        style={{ "--desktop-aspect": `${desktopSize.width} / ${desktopSize.height}` } as CSSProperties}
        // Double-click on the surround is the fastest way in and out of the
        // maximised view. Never while in takeover — there a double-click is a
        // double-click on the bot's machine.
        onDoubleClick={(event) => {
          if (takeover || event.target !== event.currentTarget) return
          layout.toggleExpanded()
        }}
      >
        {transitioning ? (
          <DesktopBoot
            botName={bot.name}
            phase={state === "starting" ? "starting" : "stopping"}
            since={desktop.transitionSince}
            detail={desktop.desktop?.last_error ? null : undefined}
            onCancel={
              state === "starting" && desktop.busy === null
                ? () => void desktop.stop(false).catch(() => undefined)
                : undefined
            }
          />
        ) : !showStage ? (
          <EmptyState
            glyph="monitor"
            title="Desktop not running"
            description="Start a lightweight Linux desktop for this bot. Watch live, take over to sign in, then hand it back."
            /*
              The action people came here for, on the surface that says there is
              nothing to see. It was previously only in the lifecycle row below
              the fold, next to four other buttons of equal weight.
            */
            actionLabel={canStart ? "Start the desktop" : undefined}
            onAction={canStart ? () => void desktop.start().catch(() => undefined) : undefined}
          />
        ) : canvasMode ? (
          <div className="desktop-stage" style={stageStyle}>
            <canvas
              ref={canvasRef}
              className={cx("desktop-canvas", takeover && "desktop-canvas--live")}
              style={stageStyle}
              tabIndex={takeover ? 0 : -1}
              role="img"
              aria-label={
                takeover
                  ? "Bot desktop screenshot — you have control. Click to click, type to type. Escape releases."
                  : "Bot desktop screenshot, read only"
              }
              onClick={onCanvasClick}
              onContextMenu={onCanvasContextMenu}
              onKeyDown={onCanvasKeyDown}
              onFocus={() => setCanvasFocused(true)}
              onBlur={() => setCanvasFocused(false)}
            />
            <span className="desktop-marker" ref={markerRef} aria-hidden="true">
              <span className="desktop-marker__ring" />
              <span className="desktop-marker__dot" />
            </span>
            {!shots.frame ? (
              <div className="canvas-overlay">
                {shots.error ? (
                  <ErrorState error={shots.error} title="No screenshot" compact />
                ) : (
                  <Spinner inline label="Waiting for the first frame…" />
                )}
              </div>
            ) : null}
          </div>
        ) : streamUrl ? (
          <div className="desktop-stage" style={stageStyle}>
            <iframe
              className="stream-frame"
              title={`${bot.name} desktop stream`}
              src={streamUrl}
              style={stageStyle}
              onLoad={() => setIframeLoaded(true)}
              onError={() => setIframeFailed(true)}
              allow="clipboard-read; clipboard-write"
            />
          </div>
        ) : (
          <div className="canvas-overlay">
            {stream.error ? (
              <ErrorState
                error={stream.error}
                title="Could not open the desktop stream"
                onRetry={stream.refresh}
                compact
              />
            ) : (
              <Spinner inline label="Opening the live stream…" />
            )}
          </div>
        )}
      </div>

      {/*
        The one thing that must never be ambiguous: where the next keystroke
        goes. Three states, each with its own colour role and its own sentence —
        not in control, in control but the chat box has focus, in control with
        the keyboard genuinely pointed at the bot's machine.
      */}
      <div
        className={cx(
          "desktop-inputbar",
          takeover && "desktop-inputbar--takeover",
          keyboardLive && "desktop-inputbar--live",
        )}
        role="status"
        aria-live="polite"
      >
        <span className="desktop-inputbar__dot" aria-hidden="true" />
        <span className="desktop-inputbar__text">
          {!takeover
            ? "Watching — the bot is driving."
            : keyboardLive
              ? `Your keyboard is going to ${bot.name}'s machine.`
              : "You have control — click the desktop to type into it."}
        </span>
        {takeover ? (
          <span className="desktop-inputbar__hint">
            <kbd>Esc</kbd> releases
          </span>
        ) : null}
      </div>

      <div className="desktop-footer">
        <div className="desktop-footer__row">
          <button
            type="button"
            className={cx("btn", "btn--sm", takeover ? "btn--danger" : "btn--primary")}
            disabled={state !== "running" && state !== "suspended"}
            onClick={() => setTakeoverMode(!takeover)}
            aria-pressed={takeover}
          >
            {takeover ? "Hand back" : "Take over"}
          </button>

          {takeover ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={keyboardLive ? releaseKeyboard : () => canvasRef.current?.focus()}
            >
              <Icon name="keyboard" size={15} />
              {keyboardLive ? "Release keyboard" : "Grab keyboard"}
            </button>
          ) : null}

          {/*
            Which feed you are watching, as a two-state control rather than a
            button labelled with the state you are not in.

            "Use live stream" told you what pressing it would do and left the
            only way of knowing which mode was actually on to the monospace
            meta text at the far end of the row. That is the same mistake the
            header already avoids with Fit / 1:1, and it is worse here, because
            the difference between a live stream and a screenshot fallback is
            the difference between watching a machine and watching a photograph
            of one. Same `segmented` component, so the pane has one vocabulary
            for "pick a view" instead of two.
          */}
          <div className="segmented" role="group" aria-label="Desktop feed">
            <button
              type="button"
              className={cx("segmented__option", !canvasMode && "segmented__option--on")}
              aria-pressed={!canvasMode}
              disabled={state !== "running"}
              title="Watch the desktop live over the stream proxy"
              onClick={() => {
                if (!canvasMode) return
                setIframeFailed(false)
                setIframeLoaded(false)
                setMode("stream")
                // The previous ticket is single-use and 60 seconds long, so
                // going back to the live view always needs a new one.
                stream.refresh()
              }}
            >
              Live
            </button>
            <button
              type="button"
              className={cx("segmented__option", canvasMode && "segmented__option--on")}
              aria-pressed={canvasMode}
              title="Fall back to one screenshot a second"
              onClick={() => {
                if (canvasMode) return
                setIframeFailed(false)
                setIframeLoaded(false)
                setMode("canvas")
              }}
            >
              Frames
            </button>
          </div>

          <span className="desktop-footer__meta">
            {canvasMode
              ? shots.frame
                ? `${desktopSize.width}×${desktopSize.height} · ${scale}% · ${relativeTime(shots.updatedAt)}`
                : "waiting for a frame"
              : iframeLoaded
                ? `${desktopSize.width}×${desktopSize.height} · ${scale}%`
                : "connecting…"}
          </span>
        </div>

        {takeover ? (
          <div className="desktop-footer__row">
            <label className="sr-only" htmlFor="desktop-type">
              Text to type on the bot desktop
            </label>
            <input
              id="desktop-type"
              className="input"
              placeholder="Type text, then Enter to send to the desktop"
              value={typeBuffer}
              onChange={(event) => setTypeBuffer(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault()
                  sendTypeBuffer()
                }
              }}
            />
            <button type="button" className="btn btn--ghost btn--sm" onClick={sendTypeBuffer}>
              Send text
            </button>

            {/*
              The keys, labelled as keys.

              This row was "Send text", a bare "⏎" glyph and "Send Esc" — three
              buttons of identical weight, one of which said nothing a person
              could read out loud, and no indication that the middle two are a
              different kind of thing from the first. They are a keypad: they
              press a key on the machine in the picture. So they are grouped and
              named, the group says what it does, and Tab joins them because a
              sign-in form is the single most common reason anybody is in
              takeover at all and it is the one key you cannot type.
            */}
            <span className="desktop-keys" role="group" aria-label="Send a key to the bot desktop">
              <span className="desktop-keys__label" aria-hidden="true">
                Press
              </span>
              {KEY_SENDS.map((key) => (
                <button
                  key={key.label}
                  type="button"
                  className="desktop-keys__key"
                  onClick={() => void perform({ action: "key", keys: [key.code] })}
                  title={key.title}
                >
                  {key.label}
                </button>
              ))}
            </span>
          </div>
        ) : null}

        {desktop.actionError ? (
          <div className="inline-error" role="alert">
            <span>{desktop.actionError}</span>
            <button type="button" className="btn btn--ghost btn--xs" onClick={desktop.clearActionError}>
              Dismiss
            </button>
          </div>
        ) : null}
      </div>

      <RoutineRecorder
        bot={bot}
        takeover={takeover}
        capturePaused={request !== null}
        onRequestTakeover={() => setTakeoverMode(true, false)}
      />
    </section>
  )
}
