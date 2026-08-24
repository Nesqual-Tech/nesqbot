/**
 * The way back to a parked task.
 *
 * `TakeoverCard` lives inside the Bot Desktop pane, which only ever shows one
 * bot. So the moment somebody switches teammates, presses "Not now", or
 * reopens the app after closing it, the card is not on screen — and a run
 * waiting on a human that nobody can find is worse than no handoff at all.
 *
 * This is the pill that keeps it findable: fixed, above the maximised pane,
 * and it says whose task and why. Clicking it selects that bot, which puts the
 * card back in front of the person.
 *
 * It stacks above the approvals hint rather than replacing it — the two are
 * different asks and either can be outstanding.
 */
import { useMemo } from "react"
import { cx } from "../lib/format"
import { inlineText, parseInline } from "../lib/markdown"
import type { TakeoverRequest } from "../lib/takeover"
import { Icon } from "./Icon"

export interface TakeoverBeaconProps {
  /** Requests that are *not* currently visible as a card. */
  requests: TakeoverRequest[]
  /** Resolves a display name once the bot list has loaded. */
  nameFor: (request: TakeoverRequest) => string
  onOpen: (request: TakeoverRequest) => void
  /** Nudges the pill up when the approvals hint is also on screen. */
  stacked?: boolean
}

export function TakeoverBeacon({ requests, nameFor, onOpen, stacked = false }: TakeoverBeaconProps) {
  const primary = requests[0] ?? null

  /*
   * The reason, with its Markdown flattened away rather than rendered.
   *
   * Everywhere else the bot's own sentence gets rendered (`TakeoverCard`,
   * `MessageBubble`), but this is the inside of a `<button>` and a `title`
   * attribute. A link in there would be an interactive element nested in an
   * interactive element, and the title cannot carry markup at all. Flattening
   * keeps the teaser readable — no stray `**` — without either problem.
   *
   * ## Why both hooks are above the early return, and must stay there
   *
   * This component shipped with `useMemo` for `primary` *before* `if (!primary)
   * return null` and `useMemo` for `reason` *after* it. That is a conditional
   * hook, and it is the blank-screen bug the owner reported.
   *
   * With nothing parked the component renders one hook and returns null. The
   * moment a run parks on a human it renders two, React raises "Rendered more
   * hooks than during the previous render", and — because `<TakeoverBeacon>`
   * sits directly in the shell, outside every panel `ErrorBoundary` — the throw
   * unmounted the whole tree to an empty `<body>`.
   *
   * It never showed up in development because a second defect hid it: the
   * impure state updater in `state/takeover.tsx` (fixed alongside this) meant
   * `requests` never became non-empty under StrictMode, so the second hook was
   * never reached. Production has no such double-invoke, so there the list
   * filled, the beacon crashed, and the app went white — precisely at the
   * product's headline moment.
   *
   * `primary` needs no memo at all (an array index is not a computation), and
   * `reason` is now computed unconditionally with a null-safe input. One hook,
   * every render, whatever the list holds.
   */
  const reason = useMemo(() => (primary ? inlineText(parseInline(primary.reason)) : ""), [primary])

  if (!primary) return null

  const extra = requests.length - 1
  const name = nameFor(primary)

  return (
    <button
      type="button"
      className={cx("takeover-beacon", stacked && "takeover-beacon--stacked")}
      onClick={() => onOpen(primary)}
      data-testid="takeover-beacon"
      data-run-id={primary.runId}
      title={reason}
    >
      <span className="takeover-beacon__pip" aria-hidden="true" />
      <Icon name="user" size={15} />
      <span className="takeover-beacon__text">
        {name} needs you — {reason}
      </span>
      {extra > 0 ? <span className="takeover-beacon__count">+{extra}</span> : null}
    </button>
  )
}
