/**
 * How big the Bot Desktop pane is, and where that preference is kept.
 *
 * The pane used to be a fixed 440px column showing a 1440×900 desktop at 30%,
 * which is unreadable and unclickable — and taking over to finish a sign-in is
 * the single most important thing a person does in this app. So the width is
 * draggable, there is a maximised mode that covers the window, and all of it
 * survives a restart: somebody who works in takeover should not have to
 * re-expand the pane every morning.
 *
 * Same storage shape and same defensive parse as `state/AppState`'s selection
 * memory — a corrupt value must degrade to the default, never to a blank app.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { clamp } from "../lib/desktopGeometry"

/** How the desktop is scaled inside the pane. */
export type DesktopZoom = "fit" | "actual"

export interface DesktopLayoutApi {
  /** Docked width in CSS pixels. Ignored while `expanded`. */
  width: number
  minWidth: number
  maxWidth: number
  /** Live drag: writes the CSS variable straight to the DOM, no re-render. */
  previewWidth: (width: number) => void
  /** End of drag (or a keyboard nudge): clamp, re-render, persist. */
  commitWidth: (width: number) => void
  resetWidth: () => void
  resizing: boolean
  setResizing: (value: boolean) => void
  /** Maximised — the pane covers the whole window. */
  expanded: boolean
  setExpanded: (value: boolean) => void
  toggleExpanded: () => void
  zoom: DesktopZoom
  setZoom: (zoom: DesktopZoom) => void
  toggleZoom: () => void
  /** Attach to the app shell; the width lands on it as a custom property. */
  shellRef: (node: HTMLElement | null) => void
}

const STORAGE_KEY = "nesq.desktopPane"
const WIDTH_VAR = "--desktop-pane-width"

export const DESKTOP_PANE_DEFAULT_WIDTH = 440
export const DESKTOP_PANE_MIN_WIDTH = 340

/**
 * The rest of the app still has to be usable while the pane is wide, so the
 * sidebar plus a legible chat column is reserved. Below that the pane simply
 * takes what is left — on a genuinely small window the answer is to maximise,
 * not to squeeze chat to nothing.
 */
const RESERVED_FOR_REST = 660

interface StoredLayout {
  width?: number
  expanded?: boolean
  zoom?: DesktopZoom
}

function readStored(): StoredLayout {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as StoredLayout
    return typeof parsed === "object" && parsed !== null ? parsed : {}
  } catch {
    return {}
  }
}

function maxWidthFor(viewportWidth: number): number {
  return Math.max(DESKTOP_PANE_MIN_WIDTH, Math.round(viewportWidth - RESERVED_FOR_REST))
}

export function useDesktopLayout(): DesktopLayoutApi {
  const stored = useRef<StoredLayout>(readStored())
  const shell = useRef<HTMLElement | null>(null)

  const [maxWidth, setMaxWidth] = useState(() => maxWidthFor(typeof window === "undefined" ? 1440 : window.innerWidth))
  const [width, setWidth] = useState(() => {
    const initial = stored.current.width ?? DESKTOP_PANE_DEFAULT_WIDTH
    return Number.isFinite(initial) ? Math.round(initial) : DESKTOP_PANE_DEFAULT_WIDTH
  })
  const [expanded, setExpandedState] = useState(() => stored.current.expanded === true)
  const [zoom, setZoomState] = useState<DesktopZoom>(() => (stored.current.zoom === "actual" ? "actual" : "fit"))
  const [resizing, setResizing] = useState(false)

  // A window that got narrower must not leave a pane wider than the window.
  useEffect(() => {
    const onResize = () => setMaxWidth(maxWidthFor(window.innerWidth))
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [])

  const effectiveWidth = clamp(width, DESKTOP_PANE_MIN_WIDTH, maxWidth)

  /*
   * The width lives on the shell as a custom property rather than in a style
   * prop, because a pointer drag emits a move event per frame and re-rendering
   * the whole shell (chat transcript included) at 120Hz is visibly worse than
   * the fixed pane it replaces. Dragging writes the variable directly; React
   * only hears about it on pointerup.
   */
  const applyWidth = useCallback((value: number) => {
    shell.current?.style.setProperty(WIDTH_VAR, `${Math.round(value)}px`)
  }, [])

  const shellRef = useCallback(
    (node: HTMLElement | null) => {
      shell.current = node
      if (node) applyWidth(clamp(width, DESKTOP_PANE_MIN_WIDTH, maxWidthFor(window.innerWidth)))
    },
    [applyWidth, width],
  )

  useEffect(() => {
    applyWidth(effectiveWidth)
  }, [applyWidth, effectiveWidth])

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ width: effectiveWidth, expanded, zoom }))
    } catch {
      /* a full or blocked store is not worth a broken pane */
    }
  }, [effectiveWidth, expanded, zoom])

  const previewWidth = useCallback(
    (value: number) => applyWidth(clamp(value, DESKTOP_PANE_MIN_WIDTH, maxWidth)),
    [applyWidth, maxWidth],
  )

  const commitWidth = useCallback(
    (value: number) => setWidth(Math.round(clamp(value, DESKTOP_PANE_MIN_WIDTH, maxWidth))),
    [maxWidth],
  )

  return useMemo<DesktopLayoutApi>(
    () => ({
      width: effectiveWidth,
      minWidth: DESKTOP_PANE_MIN_WIDTH,
      maxWidth,
      previewWidth,
      commitWidth,
      resetWidth: () => setWidth(DESKTOP_PANE_DEFAULT_WIDTH),
      resizing,
      setResizing,
      expanded,
      setExpanded: setExpandedState,
      toggleExpanded: () => setExpandedState((value) => !value),
      zoom,
      setZoom: setZoomState,
      toggleZoom: () => setZoomState((value) => (value === "fit" ? "actual" : "fit")),
      shellRef,
    }),
    [effectiveWidth, maxWidth, previewWidth, commitWidth, resizing, expanded, zoom, shellRef],
  )
}
