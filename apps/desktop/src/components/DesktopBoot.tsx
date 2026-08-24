/**
 * The Bot Desktop pane while there is no desktop to look at yet.
 *
 * ## What it replaces
 *
 * `state === "starting"` used to fall through to the ordinary stage, where the
 * screenshot feed is switched off (it only polls a `running` or `suspended`
 * desktop) and the canvas therefore showed a spinner captioned "Waiting for the
 * first frame…". For up to three minutes. A spinner that cannot succeed is
 * worse than no spinner: it is the state in which people close the app,
 * reopen it, and press Start again — which on ACI queues a second boot behind
 * the first and makes the wait they were complaining about longer.
 *
 * ## What it says instead
 *
 * Three facts and no invention: which stage the boot is in, how long it has
 * been going, and roughly how much longer a normal one takes. Plus the way out
 * — Stop is right there, because "this is taking too long, cancel it" is a
 * legitimate response to a three-minute wait and hunting for the button is not
 * part of the experience anyone wants.
 *
 * The clock is written straight to the DOM once a second rather than held in
 * state: the pane re-renders a canvas, a screenshot feed and a recorder, and
 * none of that needs to happen because a digit changed.
 */
import { useEffect, useRef, type CSSProperties } from "react"
import {
  bootEta,
  bootFillPercent,
  bootStage,
  DESKTOP_TICK_PERCENT,
  DESKTOP_WORST_S,
  stopStage,
} from "../lib/desktopBoot"
import { Icon } from "./Icon"

export interface DesktopBootProps {
  botName: string
  /** `starting` or `stopping` — the two transitional lifecycle states. */
  phase: "starting" | "stopping"
  /** When this client first saw the transition. Null falls back to zero. */
  since: number | null
  /** The server's own explanation, when it has sent one. Always wins. */
  detail?: string | null
  /** Abandon the boot. Omitted while a lifecycle call is already in flight. */
  onCancel?: () => void
}

/** `0:42`. Minutes always, so the width does not jump at ten seconds. */
function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`
}

export function DesktopBoot({ botName, phase, since, detail, onCancel }: DesktopBootProps) {
  const clockRef = useRef<HTMLSpanElement | null>(null)
  const barRef = useRef<HTMLSpanElement | null>(null)
  const stageRef = useRef<HTMLParagraphElement | null>(null)
  const etaRef = useRef<HTMLSpanElement | null>(null)
  const startedAt = since ?? Date.now()
  const booting = phase === "starting"

  useEffect(() => {
    const paint = () => {
      const seconds = (Date.now() - startedAt) / 1000
      if (clockRef.current) clockRef.current.textContent = clock(Date.now() - startedAt)
      if (stageRef.current) {
        stageRef.current.textContent = detail ?? (booting ? bootStage(seconds) : stopStage(seconds))
      }
      if (etaRef.current) etaRef.current.textContent = booting ? bootEta(seconds) : ""
      if (barRef.current) {
        barRef.current.style.setProperty("--boot-fill", `${bootFillPercent(seconds)}%`)
        barRef.current.setAttribute("aria-valuenow", String(Math.round(seconds)))
      }
    }
    paint()
    const timer = setInterval(paint, 1000)
    return () => clearInterval(timer)
  }, [startedAt, detail, booting])

  return (
    <div className="desktop-boot" role="status" aria-live="polite">
      <div className="desktop-boot__head">
        <Icon name="monitor" size={18} />
        <h3 className="desktop-boot__title">
          {booting ? `Starting ${botName}'s desktop` : `Stopping ${botName}'s desktop`}
        </h3>
        <span className="desktop-boot__clock" ref={clockRef} aria-hidden="true">
          0:00
        </span>
      </div>

      <p className="desktop-boot__stage" ref={stageRef}>
        {detail ?? (booting ? bootStage(0) : stopStage(0))}
      </p>

      {booting ? (
        <>
          <span
            className="boot-bar boot-bar--lg"
            ref={barRef}
            style={{ "--boot-fill": "2%", "--boot-tick": `${DESKTOP_TICK_PERCENT}%` } as CSSProperties}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={DESKTOP_WORST_S}
            aria-valuenow={0}
            aria-label="Desktop start progress"
          >
            <span className="boot-bar__fill" />
            <span className="boot-bar__tick" />
          </span>
          <div className="desktop-boot__facts">
            <span className="desktop-boot__eta" ref={etaRef}>
              {bootEta(0)}
            </span>
            <span className="desktop-boot__note">
              A first boot pulls the desktop image, which is the slow part. The next one is quick.
            </span>
          </div>
        </>
      ) : null}

      {onCancel ? (
        <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel}>
          {booting ? "Cancel the start" : "Stopping…"}
        </button>
      ) : null}
    </div>
  )
}
