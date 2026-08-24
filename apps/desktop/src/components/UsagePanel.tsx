import { useEffect, useMemo, useRef, useState } from "react"
import { errorMessage } from "../api/client"
import { useUsage, type TierBreakdown } from "../hooks/useUsage"
import { compactNumber, cx, plural, pct, usd, usdSmart } from "../lib/format"
import { dur, ease, gsap, stagger, useGSAP } from "../lib/motion"
import { useToast } from "../state/AppState"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import type { UsageRow } from "../types"

export interface UsagePanelProps {
  /** Bumping this refetches — the shell does it after every completed turn. */
  refreshKey?: number
}

/* ------------------------------------------------------------------ *
 * What a row means
 * ------------------------------------------------------------------ */

/**
 * Four states, not two.
 *
 * The panel used to know `warn` (>80%) and `over` (>=100%) and nothing else,
 * which had two problems. The first is arithmetic: `pct` returns a float and
 * the chip rounded it, so Vesna at $17.95 of $18.00 — five cents of headroom
 * left, and genuinely still inside her cap — was labelled "100% of budget".
 * For a product sold on its handling of money, reporting a bot as having spent
 * its whole budget when it has not is exactly the wrong kind of wrong.
 *
 * The second is `uncapped`. A bot with `budget_usd` of zero rendered an empty
 * meter and the reassuring sentence "$0.00 of $0.00 today", when what is
 * actually true is that nothing stops it. That is a governance fact and it
 * belongs in the same slot as "over cap", not hidden in a division by zero.
 */
type SpendState = "over" | "uncapped" | "near" | "ok"

const STATE_WORD: Record<SpendState, string> = {
  over: "Over cap",
  uncapped: "No cap",
  near: "Near cap",
  ok: "",
}

interface RowFacts {
  row: UsageRow
  spent: number
  budget: number
  /** 0–999, unrounded. Only ever floored for display. */
  percent: number
  state: SpendState
  /** The consequence line: how much room is left, or how far past it is. */
  headroom: string
}

function factsFor(row: UsageRow): RowFacts {
  const spent = Number(row.spent_usd_today) || 0
  const budget = Number(row.budget_usd) || 0
  const percent = pct(spent, budget)

  if (budget <= 0) {
    return {
      row,
      spent,
      budget,
      percent: spent > 0 ? 100 : 0,
      state: "uncapped",
      headroom: "No daily cap. Nothing stops this bot spending.",
    }
  }

  // Strictly greater: spending your cap exactly is not spending past it.
  if (spent > budget) {
    return { row, spent, budget, percent, state: "over", headroom: `${usdSmart(spent - budget)} past the daily cap.` }
  }

  const left = `${usdSmart(budget - spent)} left today.`
  return { row, spent, budget, percent, state: percent >= 80 ? "near" : "ok", headroom: left }
}

/** Worst first, then by how close to the cap. The same rule as the approval queue. */
const STATE_RANK: Record<SpendState, number> = { over: 0, uncapped: 1, near: 2, ok: 3 }

function worstFirst(a: RowFacts, b: RowFacts): number {
  return (
    STATE_RANK[a.state] - STATE_RANK[b.state] ||
    b.percent - a.percent ||
    a.row.bot_name.localeCompare(b.row.bot_name)
  )
}

/* ------------------------------------------------------------------ *
 * Spend, as it happens
 * ------------------------------------------------------------------ */

/**
 * What the last turn cost, per bot.
 *
 * The shell already refetches this panel after every completed turn, so the
 * numbers were correct and completely silent: a row would go from $4.82 to
 * $5.31 between two renders with nothing to say a thing had happened. Money
 * leaving is the one number in this product a person is entitled to see move.
 *
 * So the difference is held and shown, for a while, next to the bot that spent
 * it. Derived by diffing consecutive payloads rather than from a new API field:
 * "since you last looked" is a claim this client can actually make on its own.
 *
 * The first payload is deliberately not a change — arriving at the panel is not
 * a bot spending eleven dollars in front of you.
 */
const DELTA_HOLD_MS = 9000

function useSpendDeltas(rows: UsageRow[]): Record<string, number> {
  const previous = useRef<Record<string, number> | null>(null)
  const timers = useRef<Record<string, number>>({})
  const [deltas, setDeltas] = useState<Record<string, number>>({})

  useEffect(() => {
    const now: Record<string, number> = {}
    for (const row of rows) now[row.bot_id] = Number(row.spent_usd_today) || 0

    const before = previous.current
    previous.current = now
    if (!before) return

    const grew: Record<string, number> = {}
    for (const [botId, value] of Object.entries(now)) {
      // A bot absent from the previous payload has not "spent" its whole total.
      if (!(botId in before)) continue
      const delta = value - before[botId]
      // Half a hundredth of a cent: below this it is float noise, not spend.
      if (delta > 0.00005) grew[botId] = delta
    }
    if (Object.keys(grew).length === 0) return

    setDeltas((prev) => ({ ...prev, ...grew }))
    for (const botId of Object.keys(grew)) {
      window.clearTimeout(timers.current[botId])
      timers.current[botId] = window.setTimeout(() => {
        setDeltas((prev) => {
          if (!(botId in prev)) return prev
          const next = { ...prev }
          delete next[botId]
          return next
        })
      }, DELTA_HOLD_MS)
    }
  }, [rows])

  /*
   * The hold is a timeout rather than a GSAP delay on purpose. `delay()` in
   * `lib/motion` collapses to zero under reduced motion, which is right for an
   * animation offset and wrong for this: somebody who has asked for less
   * movement has not asked to be told less. The chip's *entrance* is the part
   * that is motion, and that goes through `dur()` like everything else.
   */
  useEffect(() => {
    const pending = timers.current
    return () => {
      for (const timer of Object.values(pending)) window.clearTimeout(timer)
    }
  }, [])

  return deltas
}

/* ------------------------------------------------------------------ *
 * Pieces
 * ------------------------------------------------------------------ */

function BudgetEditor({ row, onSave, onDone }: { row: UsageRow; onSave: (value: number) => Promise<void>; onDone: () => void }) {
  const [value, setValue] = useState(String(row.budget_usd ?? 0))
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (!dirty) setValue(String(row.budget_usd ?? 0))
  }, [row.budget_usd, dirty])

  const parsed = Number(value)
  const valid = Number.isFinite(parsed) && parsed >= 0

  return (
    <div className="budget-editor">
      <label className="field field--inline">
        <span className="field__label">New cap</span>
        <input
          className={cx("input", "input--tiny", !valid && "input--invalid")}
          value={value}
          inputMode="decimal"
          autoFocus
          aria-invalid={!valid}
          onChange={(event) => {
            setValue(event.target.value)
            setDirty(true)
          }}
        />
      </label>
      <button
        type="button"
        className="btn btn--primary btn--sm"
        disabled={!valid || saving || !dirty}
        onClick={async () => {
          setSaving(true)
          try {
            await onSave(parsed)
            setDirty(false)
            onDone()
          } finally {
            setSaving(false)
          }
        }}
      >
        {saving ? <Spinner inline label="Saving" /> : "Save cap"}
      </button>
      <button type="button" className="btn btn--ghost btn--sm" onClick={onDone}>
        Cancel
      </button>
    </div>
  )
}

interface UsageCardProps {
  facts: RowFacts
  tiers: TierBreakdown[]
  delta?: number
  onSaveBudget: (value: number) => Promise<void>
}

function UsageCard({ facts, tiers, delta, onSaveBudget }: UsageCardProps) {
  const [ledgerOpen, setLedgerOpen] = useState(false)
  const [capOpen, setCapOpen] = useState(false)
  const { row, spent, budget, percent, state, headroom } = facts

  const calls = tiers.reduce((total, tier) => total + tier.calls, 0)
  const ledgerId = `usage-ledger-${row.bot_id}`
  const word = STATE_WORD[state]

  return (
    <article
      /*
       * `data-spend` is what the card wears, exactly as `data-risk` is what an
       * approval wears. The edge band, the state word and the meter fill all
       * read one attribute, so "over cap" and "fine" are different objects at
       * a glance rather than the same object with a different number in it.
       */
      data-spend={state}
      className={cx("card", "usage", delta !== undefined && "usage--ticked")}
      aria-label={`${row.bot_name} spend today`}
    >
      <header className="usage__header">
        <div className="usage__lead">
          {word ? <div className="usage__state-word">{word}</div> : null}
          <h3 className="usage__name">{row.bot_name}</h3>
        </div>
        <div className="usage__figures">
          <span className="usage__spent">{usdSmart(spent)}</span>
          <span className="usage__cap">{budget > 0 ? `of ${usd(budget)}` : "uncapped"}</span>
        </div>
      </header>

      <div
        className="usage-bar"
        role="meter"
        aria-valuenow={Math.floor(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${row.bot_name} budget used`}
      >
        <span className="usage-bar__fill" style={{ width: `${Math.min(100, percent)}%` }} />
      </div>

      <p className="usage__headroom">
        {headroom}
        {budget > 0 ? <span className="usage__percent">{Math.floor(percent)}%</span> : null}
        {/*
          The money that moved while you were watching. Placed on the headroom
          line rather than beside the total, because the fact it modifies is
          "how much room is left", not "how much has been spent".
        */}
        {delta !== undefined ? (
          <span className="usage__delta" role="status">
            +{usdSmart(delta)} just now
          </span>
        ) : null}
      </p>

      <div className="usage__more">
        <button
          type="button"
          className="disclosure"
          aria-expanded={ledgerOpen}
          aria-controls={ledgerId}
          disabled={calls === 0}
          onClick={() => setLedgerOpen((prev) => !prev)}
        >
          <Icon name={ledgerOpen ? "collapse" : "expand"} size={14} />
          {calls === 0 ? "No calls yet today" : ledgerOpen ? "Hide the ledger" : `Show the ${plural(calls, "call")}`}
        </button>
        <button type="button" className="disclosure" aria-expanded={capOpen} onClick={() => setCapOpen((prev) => !prev)}>
          <Icon name="chart" size={14} />
          {capOpen ? "Keep the cap" : budget > 0 ? "Change the cap" : "Set a cap"}
        </button>
      </div>

      {capOpen ? <BudgetEditor row={row} onSave={onSaveBudget} onDone={() => setCapOpen(false)} /> : null}

      {ledgerOpen && tiers.length > 0 ? (
        <div className="reveal" id={ledgerId}>
          <table className="table">
            <caption className="sr-only">Today&apos;s ledger by model tier for {row.bot_name}</caption>
            <thead>
              <tr>
                <th scope="col">Tier</th>
                <th scope="col">Calls</th>
                <th scope="col">In</th>
                <th scope="col">Out</th>
                <th scope="col">Cost</th>
              </tr>
            </thead>
            <tbody>
              {tiers.map((tier) => (
                <tr key={tier.tier}>
                  <th scope="row">
                    <code>{tier.tier}</code>
                  </th>
                  <td>{tier.calls}</td>
                  <td>{compactNumber(tier.inputTokens)}</td>
                  <td>{compactNumber(tier.outputTokens)}</td>
                  <td>{usdSmart(tier.costUsd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </article>
  )
}

/* ------------------------------------------------------------------ *
 * The panel
 * ------------------------------------------------------------------ */

export function UsagePanel({ refreshKey = 0 }: UsagePanelProps) {
  const usage = useUsage(1)
  const toast = useToast()
  const { refetch } = usage
  const bodyRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (refreshKey > 0) void refetch()
  }, [refreshKey, refetch])

  const deltas = useSpendDeltas(usage.rows)

  const facts = useMemo(() => usage.rows.map(factsFor).sort(worstFirst), [usage.rows])

  const attention = useMemo(() => {
    const counts: Record<SpendState, number> = { over: 0, uncapped: 0, near: 0, ok: 0 }
    for (const row of facts) counts[row.state] += 1
    return counts
  }, [facts])

  const totalPercent = pct(usage.totalSpend, usage.totalBudget)

  /*
   * The one number that moved, animated; nothing else.
   *
   * Keyed on which bots have an outstanding delta, so the chip animates when it
   * arrives and the four cards around it stay perfectly still. Under reduced
   * motion `dur()` is already 0ms, so the chip simply appears — which is the
   * correct behaviour, not a degraded one.
   */
  const deltaKey = Object.keys(deltas).sort().join(",")
  useGSAP(
    () => {
      if (!deltaKey) return
      gsap.from(".usage__delta", {
        y: -4,
        autoAlpha: 0,
        duration: dur("base"),
        ease: ease("entrance"),
        stagger: stagger(0.04),
      })
    },
    { dependencies: [deltaKey], scope: bodyRef },
  )

  const saveBudget = async (botId: string, value: number) => {
    try {
      await usage.setBudget(botId, value)
      toast.success("Daily cap updated", usd(value))
    } catch (err) {
      toast.error("Could not update the cap", errorMessage(err))
    }
  }

  const showSummary = !usage.initialising && facts.length > 0

  return (
    <section className="panel" id="panel-usage" role="tabpanel" aria-labelledby="nav-tab-usage">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Spend</div>
          <h2 className="panel__title">Usage</h2>
          <p className="panel__subtitle">Every model call your teammates made today, and what it cost.</p>
        </div>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => void usage.refetch()}
          disabled={usage.loading}
        >
          {usage.loading ? <Spinner inline label="Refreshing" /> : "Refresh"}
        </button>
      </header>

      {/*
        The day, before you read any of it.

        Modelled on the approvals queue summary and for the same reason: the
        headline of a spend panel should be its worst row. "$34.29 of $70.00"
        used to be a sentence in the subtitle, in muted grey, at caption size,
        which is where a product puts something it does not think you need.
      */}
      {showSummary ? (
        <div className="queue-summary summary-strip">
          <p className="queue-summary__count">
            <strong>{usdSmart(usage.totalSpend)}</strong>
            spent today
            {usage.totalBudget > 0 ? ` of ${usd(usage.totalBudget)} capped` : ""}
          </p>
          <ul className="queue-summary__tally">
            {attention.over > 0 ? (
              <li className="queue-summary__item" data-tone="danger">
                <span className="queue-summary__pip" />
                <span className="queue-summary__n">{attention.over}</span>
                <span className="queue-summary__label">over cap</span>
              </li>
            ) : null}
            {attention.uncapped > 0 ? (
              <li className="queue-summary__item" data-tone="danger">
                <span className="queue-summary__pip" />
                <span className="queue-summary__n">{attention.uncapped}</span>
                <span className="queue-summary__label">uncapped</span>
              </li>
            ) : null}
            {attention.near > 0 ? (
              <li className="queue-summary__item" data-tone="warning">
                <span className="queue-summary__pip" />
                <span className="queue-summary__n">{attention.near}</span>
                <span className="queue-summary__label">near cap</span>
              </li>
            ) : null}
            {attention.over === 0 && attention.near === 0 && attention.uncapped === 0 ? (
              <li className="queue-summary__item" data-tone="ok">
                <span className="queue-summary__pip" />
                <span className="queue-summary__label">every bot inside its cap</span>
              </li>
            ) : null}
          </ul>
          {usage.totalBudget > 0 ? (
            <div
              className="summary-strip__meter"
              role="meter"
              aria-valuenow={Math.floor(totalPercent)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="Today's spend against the combined daily cap"
            >
              <span className="summary-strip__fill" style={{ width: `${Math.min(100, totalPercent)}%` }} />
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="panel__body panel__body--grid" ref={bodyRef}>
        {usage.initialising ? <SkeletonCards cards={3} /> : null}

        {usage.error && usage.rows.length === 0 && !usage.initialising ? (
          <ErrorState error={usage.error} title="Usage unavailable" onRetry={() => void usage.refetch()} />
        ) : null}

        {!usage.initialising && !usage.error && usage.rows.length === 0 ? (
          <EmptyState glyph="chart" title="No spend today" description="Ledger entries appear as your bots work." />
        ) : null}

        {facts.map((entry) => (
          <UsageCard
            key={entry.row.bot_id}
            facts={entry}
            tiers={usage.breakdown(entry.row)}
            delta={deltas[entry.row.bot_id]}
            onSaveBudget={(value) => saveBudget(entry.row.bot_id, value)}
          />
        ))}
      </div>
    </section>
  )
}
