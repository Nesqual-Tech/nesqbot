/**
 * The drag handle between the chat column and the Bot Desktop pane.
 *
 * A real `separator` with a value, not a decorative bar: arrow keys move it,
 * Home restores the default, and a double-click does the same with the mouse.
 * That matters here because the thing it resizes is the surface somebody drives
 * a browser through — "make it bigger" has to be reachable without a pointer.
 *
 * Dragging deliberately does not go through React state. `onPreview` writes the
 * width straight onto the shell element and only `onCommit` re-renders, so a
 * drag stays smooth with a full chat transcript mounted next door.
 */
import { useCallback, useRef, type KeyboardEvent, type PointerEvent } from "react"
import { clamp } from "../lib/desktopGeometry"

export interface PaneSplitterProps {
  width: number
  min: number
  max: number
  onPreview: (width: number) => void
  onCommit: (width: number) => void
  onReset: () => void
  onResizingChange: (resizing: boolean) => void
  label?: string
}

const STEP = 24
const COARSE_STEP = 96

export function PaneSplitter({
  width,
  min,
  max,
  onPreview,
  onCommit,
  onReset,
  onResizingChange,
  label = "Resize the Bot Desktop pane",
}: PaneSplitterProps) {
  const drag = useRef<{ startX: number; startWidth: number; pointerId: number } | null>(null)

  const onPointerDown = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      // Primary button only; a right-click here should open nothing and move
      // nothing.
      if (event.button !== 0) return
      event.preventDefault()
      drag.current = { startX: event.clientX, startWidth: width, pointerId: event.pointerId }
      event.currentTarget.setPointerCapture(event.pointerId)
      onResizingChange(true)
    },
    [width, onResizingChange],
  )

  const onPointerMove = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const state = drag.current
      if (!state || state.pointerId !== event.pointerId) return
      // The pane is on the right, so dragging left makes it wider.
      onPreview(clamp(state.startWidth - (event.clientX - state.startX), min, max))
    },
    [onPreview, min, max],
  )

  const endDrag = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const state = drag.current
      if (!state || state.pointerId !== event.pointerId) return
      drag.current = null
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId)
      }
      onResizingChange(false)
      onCommit(clamp(state.startWidth - (event.clientX - state.startX), min, max))
    },
    [onCommit, onResizingChange, min, max],
  )

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const step = event.shiftKey ? COARSE_STEP : STEP
      if (event.key === "ArrowLeft") {
        event.preventDefault()
        onCommit(clamp(width + step, min, max))
      } else if (event.key === "ArrowRight") {
        event.preventDefault()
        onCommit(clamp(width - step, min, max))
      } else if (event.key === "Home") {
        event.preventDefault()
        onReset()
      } else if (event.key === "End") {
        event.preventDefault()
        onCommit(max)
      }
    },
    [onCommit, onReset, width, min, max],
  )

  return (
    <div
      className="pane-splitter"
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={Math.round(width)}
      aria-valuemin={Math.round(min)}
      aria-valuemax={Math.round(max)}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={onReset}
      title="Drag to resize · double-click to reset"
    >
      <span className="pane-splitter__grip" aria-hidden="true" />
    </div>
  )
}
