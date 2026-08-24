/**
 * What the agent is doing, while it is doing it.
 *
 * ## The problem this replaces
 *
 * The chat pane used to end in three unrelated widgets: a spinner captioned
 * "X is thinking…", a second dashed spinner for a turn started somewhere else,
 * and a flat `<ul>` of monospace one-liners that blinked into existence with no
 * order, no timing and no outcome. Between them they answered "is something
 * happening" and nothing else. Watching a teammate work and watching a spinner
 * are different experiences, and that was the spinner.
 *
 * This is one object instead of three: a run rail. It answers, in order, the
 * questions somebody actually has while a multi-step desktop task is going.
 *
 *   Who is driving?          the head, which follows `handoff`
 *   Is it still going?       the live node, and a clock that ticks
 *   What has it done?        one row per real step, newest last
 *   How long did that take?  the gap between consecutive frames
 *   Did it work?             the outcome glyph on each tool row
 *   What did it cost?        the summary, from `tier` and `cost_usd` on `done`
 *
 * ## What it deliberately does not do
 *
 * There is no progress bar and no percentage. The agent does not know how many
 * steps a task will take, so a bar would have to be invented, and an invented
 * bar is worse than no bar: it teaches people to distrust the one honest number
 * on the screen. The rail counts what has happened and times it. When the run
 * ends, it says so.
 *
 * ## Motion
 *
 * Every duration comes from `dur()`, so `prefers-reduced-motion` collapses the
 * whole thing to instant without a second code path (see `lib/motion`). Three
 * movements, each with a job:
 *
 *   1. **Steps arrive.** A row lands from just above, and a tool row lands with
 *      a beat more weight than an informational one, because a tool row is a
 *      *result* and results should register. This is the only place in the app
 *      that animates on a data event rather than on mount.
 *   2. **The live node breathes.** The one infinite loop in the product, and
 *      the only honest use of one: it means "still running". It stops the
 *      instant the run settles, which is the actual information.
 *   3. **The summary lands.** Cost and elapsed arrive together, once, with
 *      weight. It is the last thing that happens and it should feel like it.
 *
 * Nothing here gates the display of a result. Rows render at full opacity and
 * are animated *from* an offset, never *to* one, so a dropped frame or a killed
 * tween leaves readable text rather than an invisible row.
 */
import { memo, useEffect, useMemo, useRef, type CSSProperties } from "react"
import { dur, ease, gsap, loop, stagger, useGSAP, useReducedMotion } from "../lib/motion"
import {
  bootEta,
  bootFillPercent,
  bootStage,
  DESKTOP_TICK_PERCENT,
  DESKTOP_WORST_S,
} from "../lib/desktopBoot"
import { cx, usd, usdSmart } from "../lib/format"
import { Icon, type IconName } from "./Icon"
import type { StreamActivity, TurnState } from "../hooks/useMessages"

export interface AgentActivityProps {
  /** The turn record from `useMessages`. Null means nothing has run yet. */
  turn: TurnState | null
  steps: StreamActivity[]
  /** True while this window's own POST stream is open. */
  streaming: boolean
  /** Fallback name for the head, before any event has named a bot. */
  botName: string
}

/* ------------------------------------------------------------------ *
 * Formatting
 * ------------------------------------------------------------------ */

/** "840ms" / "6.2s" / "1:04" — always the shortest honest form. */
function formatElapsed(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const total = Math.round(ms / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`
}

/** The head clock, which ticks. Minutes always, so the width does not jump. */
function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`
}

const STEP_ICON: Record<StreamActivity["kind"], IconName> = {
  tool: "plug",
  handoff: "repeat",
  approval: "shield",
  takeover: "user",
  desktop: "monitor",
  cost: "chart",
  error: "alert",
  info: "spark",
}

/**
 * A desktop coming up, inside the rail.
 *
 * Same bar as the Bot Desktop pane's, from the same module, because it is the
 * same wait seen from two places — the pane when you pressed Start, the rail
 * when a bot did. See `lib/desktopBoot` for why this is the one honest place in
 * the app for a measured progress bar.
 */
function BootProgress({ seconds, detail }: { seconds: number; detail?: string }) {
  return (
    <span className="agent-run__boot">
      <span className="agent-run__boot-line">{detail ?? bootStage(seconds)}</span>
      {/*
        `--boot-fill` and `--boot-tick` are custom properties rather than a
        width, so the bar's geometry stays in the stylesheet and this supplies
        only the number.
      */}
      <span
        className="boot-bar"
        style={
          {
            "--boot-fill": `${bootFillPercent(seconds)}%`,
            "--boot-tick": `${DESKTOP_TICK_PERCENT}%`,
          } as CSSProperties
        }
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={DESKTOP_WORST_S}
        aria-valuenow={Math.round(seconds)}
        aria-valuetext={`${Math.round(seconds)} seconds into a desktop start`}
      >
        <span className="boot-bar__fill" />
        <span className="boot-bar__tick" />
      </span>
      <span className="agent-run__boot-eta">{bootEta(seconds)}</span>
    </span>
  )
}

const HEAD_STATUS: Record<TurnState["status"], { label: string; icon: IconName }> = {
  running: { label: "working", icon: "spark" },
  done: { label: "finished", icon: "check" },
  failed: { label: "failed", icon: "alert" },
  parked: { label: "waiting for you", icon: "user" },
}

/* ------------------------------------------------------------------ *
 * The ticking clock
 *
 * Written straight to the DOM rather than held in state. A `setState` once a
 * second would re-render `ChatPane`, and `ChatPane` maps the entire transcript
 * — the exact re-render the memoised `MessageBubble` exists to avoid. One
 * `textContent` write costs nothing and touches nothing else.
 * ------------------------------------------------------------------ */

function useTickingClock(startedAt: number | null, running: boolean) {
  const ref = useRef<HTMLSpanElement | null>(null)

  useEffect(() => {
    const node = ref.current
    if (!node || startedAt === null) return
    const paint = () => {
      node.textContent = formatClock(Date.now() - startedAt)
    }
    paint()
    if (!running) return
    const timer = setInterval(paint, 1000)
    return () => clearInterval(timer)
  }, [startedAt, running])

  return ref
}

/* ------------------------------------------------------------------ *
 * Component
 * ------------------------------------------------------------------ */

export const AgentActivity = memo(function AgentActivity({ turn, steps, streaming, botName }: AgentActivityProps) {
  const root = useRef<HTMLElement | null>(null)
  const reduced = useReducedMotion()

  /*
   * A turn is live when the record says so *and* something is still connected.
   * `streaming` alone misses worker-driven turns; the record alone can outlive
   * a stream that dropped without a terminal frame.
   */
  const status = turn?.status ?? "done"
  const running = turn !== null && (turn.status === "running" || streaming)
  const startedAt = turn?.startedAt ?? null
  const settled = turn !== null && !running

  const clockRef = useTickingClock(startedAt, running)

  /** Gap since the previous step, so each row says how long that bit took. */
  const rows = useMemo(
    () =>
      steps.map((step, index) => ({
        step,
        gap: index === 0 ? (startedAt === null ? null : step.at - startedAt) : step.at - steps[index - 1].at,
      })),
    [steps, startedAt],
  )

  /* --------------------------------------------------------------- *
   * 1. Steps arrive
   *
   * Only the rows that were not there last time. `seen` is the high-water
   * mark; a reset to zero rows (new thread, new turn) resets it, so the first
   * step of the next turn animates in like the first step of this one did.
   * --------------------------------------------------------------- */

  const seen = useRef(0)

  useGSAP(
    () => {
      /*
       * Queried off the root node rather than with `gsap.utils.toArray(".agent-run__step")`.
       *
       * `toArray` is a plain helper: it runs `document.querySelectorAll` and
       * knows nothing about the surrounding context's scope. Only selector
       * strings handed to a tween as *targets* get rewritten against the scope.
       * So the convenient version would reach into any other rail on the page,
       * which is not a hypothetical in an app that can mount a second transcript.
       */
      const root_ = root.current
      if (!root_) return
      const all = Array.from(root_.querySelectorAll<HTMLElement>(".agent-run__step"))
      if (all.length < seen.current) seen.current = 0
      const fresh = all.slice(seen.current)
      seen.current = all.length
      if (fresh.length === 0) return

      for (const row of fresh) {
        // A tool row is a *result* landing: a touch further to travel and a
        // glyph that pops once it gets there. Everything else is a note.
        const isResult = row.dataset.kind === "tool"
        const tl = gsap.timeline()

        tl.from(row, {
          y: isResult ? -10 : -6,
          autoAlpha: 0,
          duration: dur(isResult ? "base" : "fast"),
          ease: ease("entrance"),
        })

        const node = row.querySelector(".agent-run__node")
        if (node && isResult) {
          tl.from(node, { scale: 0.4, duration: dur("base"), ease: ease("emphasized") }, `<${stagger(0.05)}`)
        }
      }
    },
    { dependencies: [rows.length, reduced], scope: root },
  )

  /* --------------------------------------------------------------- *
   * 2. The live node breathes
   *
   * `revertOnUpdate` so the loop is torn down the moment the run settles
   * rather than left spinning against a hidden element, and `loop()` so it is
   * never created at all under reduced motion — a zero-duration tween on
   * `repeat: -1` is a busy loop, not an animation.
   * --------------------------------------------------------------- */

  useGSAP(
    () => {
      if (!running || !loop()) return
      gsap.to(".agent-run__pip", {
        scale: 1.55,
        autoAlpha: 0.25,
        duration: dur("deliberate") * 1.6,
        ease: ease("standard"),
        repeat: -1,
        yoyo: true,
      })
    },
    { dependencies: [running, reduced], scope: root, revertOnUpdate: true },
  )

  /* --------------------------------------------------------------- *
   * 3. The summary lands
   * --------------------------------------------------------------- */

  useGSAP(
    () => {
      if (!settled) return
      gsap.from(".agent-run__summary", {
        y: 6,
        scale: 0.97,
        autoAlpha: 0,
        duration: dur("slow"),
        ease: ease("entrance"),
      })
    },
    { dependencies: [settled, reduced], scope: root },
  )

  if (!turn && steps.length === 0) return null

  const head = HEAD_STATUS[status]
  const driver = turn?.botName || botName
  const totalMs = turn ? (turn.endedAt ?? Date.now()) - turn.startedAt : 0
  const toolCount = steps.filter((step) => step.kind === "tool").length

  return (
    <section
      ref={root}
      className={cx("agent-run", running && "agent-run--live")}
      data-status={status}
      aria-label={`Agent activity for ${driver}`}
    >
      <header className="agent-run__head">
        <span className="agent-run__marker" aria-hidden="true">
          {running ? <span className="agent-run__pip" /> : <Icon name={head.icon} size={13} />}
        </span>

        {/*
          While the run is live the head carries the verb and the clock, because
          that is the only place the state is written down. Once it settles both
          move to the summary, which says it precisely -- the head repeating
          "finished" next to a green check next to a summary reading "FINISHED"
          was the same fact three times.
        */}
        <span className="agent-run__who">
          <strong>{driver}</strong>
          {running ? (
            <span className="agent-run__verb">{turn?.remote ? "is working on this thread" : head.label}</span>
          ) : null}
        </span>

        {/*
          The clock is `aria-hidden` and the run has a polite live region of its
          own below. A per-second announcement of "0:07" would make the screen
          reader unusable for exactly the person who most needs to know what the
          agent is doing.
        */}
        {startedAt !== null && running ? (
          <span className="agent-run__clock" ref={clockRef} aria-hidden="true">
            0:00
          </span>
        ) : null}
      </header>

      {rows.length > 0 ? (
        <ol className="agent-run__steps">
          {rows.map(({ step, gap }) => (
            <li
              key={step.id}
              className="agent-run__step"
              data-kind={step.kind}
              data-ok={step.tool?.ok ?? step.desktop?.phase !== "unavailable"}
              data-phase={step.desktop?.phase}
            >
              <span className="agent-run__node" aria-hidden="true">
                <Icon name={step.tool ? (step.tool.ok ? "check" : "close") : STEP_ICON[step.kind]} size={11} />
              </span>

              <span className="agent-run__body">
                {step.tool ? (
                  <>
                    <span className="agent-run__connector">{step.tool.connector}</span>
                    <span className="agent-run__action">{step.tool.action}</span>
                    {step.tool.ok ? null : <span className="agent-run__failed">failed</span>}
                  </>
                ) : step.cost ? (
                  /*
                    Spend, while it is being spent. Printed as the turn's
                    running total against the day's cap rather than as this
                    step's cost alone — the question somebody watching an
                    autonomous loop has is "how much of today is left", and a
                    per-step figure of $0.0004 does not answer it.
                  */
                  <>
                    <span className="agent-run__connector">step {step.cost.step}</span>
                    <span className="agent-run__action">{usdSmart(step.cost.turnCostUsd)} this turn</span>
                    {step.cost.budgetUsd > 0 ? (
                      <span className="agent-run__budget">
                        {usdSmart(step.cost.spentTodayUsd)} of {usd(step.cost.budgetUsd)} today
                      </span>
                    ) : null}
                  </>
                ) : step.desktop?.phase === "starting" ? (
                  /*
                    The boot owns its whole row. `step.text` is not printed
                    beside it: the progress block already states the stage, and
                    rendering both put two sentences about the same wait on one
                    ellipsised line.
                  */
                  <BootProgress seconds={step.desktop.elapsedSeconds ?? 0} detail={step.desktop.detail} />
                ) : (
                  <>
                    {step.text}
                    {/*
                      The delegation path, when the server said this was one.
                      "Handed off to Sales" and "person → lead_generator →
                      sales" are the difference between a topic change and a
                      team working, and the second is the product's whole claim.
                    */}
                    {step.chain ? <span className="agent-run__chain">{step.chain}</span> : null}
                  </>
                )}
              </span>

              {gap !== null && gap >= 0 ? <span className="agent-run__gap">{formatElapsed(gap)}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}

      {settled && turn ? (
        <footer className="agent-run__summary">
          <span className="agent-run__verdict">{head.label}</span>
          <span className="agent-run__fact">
            {formatElapsed(totalMs)} <span className="agent-run__fact-label">elapsed</span>
          </span>
          {toolCount > 0 ? (
            <span className="agent-run__fact">
              {toolCount} <span className="agent-run__fact-label">{toolCount === 1 ? "tool call" : "tool calls"}</span>
            </span>
          ) : null}
          {turn.tier ? (
            <span className="agent-run__fact">
              {turn.tier} <span className="agent-run__fact-label">tier</span>
            </span>
          ) : null}
          {/*
            A real zero is worth printing: it is how a cached or tool-only turn
            proves it did not call a model. Only an absent field is hidden.
          */}
          {turn.costUsd !== null ? (
            <span className="agent-run__fact agent-run__fact--cost">
              {usd(turn.costUsd, true)} <span className="agent-run__fact-label">spend</span>
            </span>
          ) : null}
        </footer>
      ) : null}

      {/*
        One polite announcement per state change, and never the step list — a
        multi-step desktop task can emit a dozen tool frames a minute and
        reading every one aloud buries the sentence that matters.
      */}
      <p className="sr-only" role="status">
        {running
          ? `${driver} is working. ${steps.length} ${steps.length === 1 ? "step" : "steps"} so far.`
          : `${driver} ${head.label}.`}
      </p>
    </section>
  )
})
