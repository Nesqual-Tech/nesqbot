/**
 * Bottom-right toast stack. Replaces every `alert()` in the app.
 *
 * Two things changed here beyond the motion.
 *
 * **The glyphs are icons now.** They used to be the characters `•`, `✓`, `!`
 * and `⚠`. On Windows the last of those renders as a full-colour emoji, which
 * meant the warning toast ignored the palette entirely and looked different in
 * light and dark mode for no reason anyone chose. `Icon.tsx` exists precisely
 * because that had already been fixed everywhere else.
 *
 * **Toasts leave rather than vanish.** A toast that pops out of existence
 * takes the stack below it with it in the same frame, which is the single
 * cheapest-feeling moment in an app. So the viewport keeps rendering a
 * dismissed toast for as long as its exit takes: it slides out to the right
 * while its slot collapses, and the stack closes up behind it.
 *
 * That needs the viewport to hold its own copy of the list, because the
 * provider's `toasts` array drops an entry the instant it is dismissed and
 * `state/AppState` is not this lane's to change. `visible` is that copy, and
 * it is deliberately kept in the provider's order with leavers left *in place*
 * rather than appended, so nothing jumps to the bottom of the stack on its way
 * out.
 */
import { useEffect, useRef, useState } from "react"
import { dur, ease, gsap, prefersReducedMotion, stagger, useGSAP } from "../lib/motion"
import { useToast, type Toast as ToastModel, type ToastTone } from "../state/AppState"
import { Icon, type IconName } from "./Icon"

const GLYPH: Record<ToastTone, IconName> = {
  info: "spark",
  success: "check",
  warning: "alert",
  error: "alert",
}

export function ToastViewport() {
  const { toasts, dismiss } = useToast()
  const root = useRef<HTMLDivElement | null>(null)

  /*
   * The rendered list: every live toast, plus the ones on their way out, in
   * the order they have always been in.
   */
  const [visible, setVisible] = useState<ToastModel[]>([])

  useEffect(() => {
    setVisible((prev) => {
      const live = new Map(toasts.map((toast) => [toast.id, toast]))
      // Existing rows keep their slot; a row the provider has dropped stays
      // exactly where it was and is now a leaver.
      const kept = prev.map((row) => live.get(row.id) ?? row)
      const added = toasts.filter((toast) => !prev.some((row) => row.id === toast.id))
      return added.length === 0 && kept.length === prev.length ? kept : [...kept, ...added]
    })
  }, [toasts])

  const leavingIds = visible.filter((row) => !toasts.some((t) => t.id === row.id)).map((row) => row.id)
  const leavingKey = leavingIds.join(",")

  /* Arrivals. Only rows that have not been animated before. */
  const entered = useRef(new Set<string>())

  useGSAP(
    () => {
      const fresh = toasts.filter((toast) => !entered.current.has(toast.id))
      if (fresh.length === 0) return
      for (const toast of fresh) entered.current.add(toast.id)

      const nodes = fresh
        .map((toast) => root.current?.querySelector<HTMLElement>(`[data-toast-id="${toast.id}"] .toast`))
        .filter((node): node is HTMLElement => node !== null && node !== undefined)
      if (nodes.length === 0) return

      gsap.from(nodes, {
        x: 28,
        scale: 0.97,
        autoAlpha: 0,
        duration: dur("slow"),
        ease: ease("entrance"),
        stagger: stagger(0.05),
      })
    },
    { dependencies: [toasts], scope: root },
  )

  /*
   * Departures. The slot collapses so the stack closes; the toast itself
   * leaves sideways, which is the direction it came from.
   *
   * `overflow: hidden` is applied here rather than in the stylesheet because a
   * toast carries a popover shadow, and clipping it for the whole of its life
   * would shave that shadow off every edge. It only needs clipping while its
   * slot is shorter than it is.
   */
  useGSAP(
    () => {
      if (leavingIds.length === 0) return
      const finished: string[] = []
      const tl = gsap.timeline({
        onComplete: () => {
          const gone = new Set(finished)
          for (const id of gone) entered.current.delete(id)
          setVisible((prev) => prev.filter((row) => !gone.has(row.id)))
        },
      })

      for (const id of leavingIds) {
        const slot = root.current?.querySelector<HTMLElement>(`[data-toast-id="${id}"]`)
        if (!slot) continue
        finished.push(id)
        const card = slot.querySelector<HTMLElement>(".toast")
        gsap.set(slot, { overflow: "hidden" })
        if (card) {
          tl.to(card, { x: 28, autoAlpha: 0, duration: dur("fast"), ease: ease("exit") }, 0)
        }
        tl.to(
          slot,
          { height: 0, marginBottom: 0, duration: dur("base"), ease: ease("exit") },
          prefersReducedMotion() ? 0 : dur("fast") * 0.6,
        )
      }

      // Nothing matched a node (a remount, say). Drop them rather than leaving
      // rows stranded in `visible` forever.
      if (finished.length === 0) {
        const gone = new Set(leavingIds)
        setVisible((prev) => prev.filter((row) => !gone.has(row.id)))
      }
    },
    { dependencies: [leavingKey], scope: root },
  )

  if (visible.length === 0) return null

  return (
    <div className="toast-viewport" ref={root} aria-live="polite" aria-relevant="additions text">
      {visible.map((toast) => (
        <div className="toast-slot" key={toast.id} data-toast-id={toast.id}>
          <div className={`toast toast--${toast.tone}`} role={toast.tone === "error" ? "alert" : "status"}>
            <span className="toast__glyph" aria-hidden="true">
              <Icon name={GLYPH[toast.tone] ?? "spark"} size={16} />
            </span>
            <div className="toast__body">
              <div className="toast__title">{toast.title}</div>
              {toast.description ? <div className="toast__description">{toast.description}</div> : null}
            </div>
            <button
              type="button"
              className="toast__close"
              onClick={() => dismiss(toast.id)}
              aria-label={`Dismiss: ${toast.title}`}
            >
              <Icon name="close" size={14} />
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
