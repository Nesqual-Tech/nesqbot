import { useMemo } from "react"
import { approvalExecutionOutcome } from "@nesqbot/protocol"
import { riskLabels } from "@nesqbot/ui"
import { isApiError } from "../api/client"
import { riskTally } from "../lib/approvals"
import type { ApprovalsApi } from "../hooks/useApprovals"
import { useSelection, useToast } from "../state/AppState"
import { ApprovalCard } from "./ApprovalCard"
import { EmptyState, ErrorState } from "./EmptyState"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import { StandingPermissions } from "./StandingPermissions"
import { useStandingApprovals } from "../hooks/useStandingApprovals"
import type { Bot } from "../types"

export interface ApprovalsPanelProps {
  approvals: ApprovalsApi
  bots: Bot[]
}

const STATUSES = ["pending", "approved", "rejected", "expired", "all"] as const

export function ApprovalsPanel({ approvals, bots }: ApprovalsPanelProps) {
  const toast = useToast()
  const { focusApprovalId, setFocusApprovalId } = useSelection()
  /*
   * Standing permissions live on this panel and not on a settings screen of
   * their own, because they are the same question as the queue above them: the
   * queue is what is waiting on you, and this is what you have already allowed.
   * A person who has just been told "I will stop asking about this" comes here
   * to see what that means, and sending them to a different section to find out
   * would be putting a door between somebody and the thing they want to undo.
   */
  const standing = useStandingApprovals()

  const botById = useMemo(() => {
    const map: Record<string, Bot> = {}
    for (const bot of bots) map[bot.id] = bot
    return map
  }, [bots])

  /*
   * The shape of the backlog, before any of it is read.
   *
   * "4 waiting" is a count; "1 delete, 2 spend, 1 send" is a decision about
   * what to open first. The tally is ordered most-dangerous-first for exactly
   * that reason — the queue's headline should be its worst item.
   */
  /*
   * Both numbers count the same rows: the ones still waiting.
   *
   * The tally used to run over every approval in the list while the headline
   * counted only the pending ones, which agreed by accident whenever the filter
   * was `pending` and disagreed the moment it was not. Applying a decision
   * optimistically made that visible in the common case too — the headline
   * dropped to 3 in the same frame the tally still read 1 delete, 2 spend, 1
   * send. A summary whose parts do not add up is worse than no summary.
   */
  const pending = useMemo(() => approvals.approvals.filter((a) => a.status === "pending"), [approvals.approvals])
  const tally = useMemo(() => riskTally(pending), [pending])
  const waiting = pending.length

  /*
   * The toast reports the *outcome*, not the decision.
   *
   * The decision is already on screen — the card flipped, the badge dropped and
   * the queue summary shrank the moment the button was pressed. Repeating
   * "Approved" a few seconds later says nothing new. What is worth an
   * interruption is the half the person could not predict: the action refused,
   * or the task did not restart. So the success path stays quiet unless there
   * is something to say, and the failure paths are loud.
   *
   * `approvalExecutionOutcome` rather than `execution.ok`: a rejection that
   * resumed the run carries no `ok` at all, and reading that as `false` used to
   * announce every plain "no" as an execution failure.
   */
  const decide = async (id: string, decision: "approved" | "rejected", note?: string) => {
    let result
    try {
      result = await approvals.decide(id, decision, note)
    } catch (err) {
      // The card states the reason inline, next to the thing it happened to.
      // A toast as well would be the same sentence twice.
      if (focusApprovalId === id && isApiError(err) && err.isConflict) setFocusApprovalId(null)
      throw err
    }

    const outcome = approvalExecutionOutcome(result.execution)
    const continuation = result.execution?.continuation

    if (outcome === "failed") {
      toast.error("Approved, but the action failed", (result.execution as { error: string }).error)
    } else if (continuation?.error) {
      toast.warning("Decision recorded — the task did not resume", continuation.error)
    }

    /*
     * A permission acquired by this decision is worth an interruption, and the
     * list under this queue has to have it in it before the person goes looking.
     *
     * The success path is otherwise deliberately quiet — see above — and this is
     * the exception that proves the rule: acquiring a standing permission is
     * precisely the outcome a person could not have predicted from the button
     * they pressed.
     */
    if (result.execution?.standing_approval) {
      toast.info("I will stop asking about this", result.execution.standing_approval.permits)
      void standing.refetch()
    }

    if (focusApprovalId === id) setFocusApprovalId(null)
    return result
  }

  return (
    <section className="panel" id="panel-approvals" role="tabpanel" aria-labelledby="nav-tab-approvals">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Governance</div>
          <h2 className="panel__title">Approvals</h2>
          <p className="panel__subtitle">
            Anything classed <code>send</code>, <code>spend</code> or <code>delete</code> waits here.
          </p>
        </div>
        <div className="panel__header-actions">
          <label className="sr-only" htmlFor="approval-status">
            Filter by status
          </label>
          <select
            id="approval-status"
            className="select"
            value={approvals.statusFilter}
            onChange={(event) => approvals.setStatusFilter(event.target.value)}
          >
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
          <label className="sr-only" htmlFor="approval-bot">
            Filter by bot
          </label>
          <select
            id="approval-bot"
            className="select"
            value={approvals.botFilter ?? ""}
            onChange={(event) => approvals.setBotFilter(event.target.value || null)}
          >
            <option value="">All bots</option>
            {bots.map((bot) => (
              <option key={bot.id} value={bot.id}>
                {bot.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void approvals.refetch()}
            disabled={approvals.loading}
          >
            {approvals.loading ? <Spinner inline label="Refreshing" /> : "Refresh"}
          </button>
        </div>
      </header>

      {tally.length > 0 ? (
        <div className="queue-summary" role="status">
          <span className="queue-summary__count">
            <strong>{waiting}</strong> waiting on you
          </span>
          <ul className="queue-summary__tally">
            {tally.map(({ risk, count }) => (
              <li key={risk} className="queue-summary__item" data-risk={risk}>
                <span className="queue-summary__pip" aria-hidden="true" />
                <span className="queue-summary__n">{count}</span>
                <span className="queue-summary__label">{riskLabels[risk]}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/*
        Above the queue, folded shut.

        It is the shorter list and the one that answers "why did nothing stop
        for me this morning", so it belongs where a person will see it while
        triaging — and folded, because on most days the answer is "nothing", and
        a permanently open empty list is a permanently wasted screenful.
      */}
      <StandingPermissions standing={standing} bots={bots} />

      <div className="panel__body">
        {approvals.initialising ? <SkeletonCards cards={2} /> : null}

        {approvals.error && approvals.approvals.length === 0 && !approvals.initialising ? (
          <ErrorState error={approvals.error} title="Approvals unavailable" onRetry={() => void approvals.refetch()} />
        ) : null}

        {!approvals.initialising && !approvals.error && approvals.approvals.length === 0 ? (
          <EmptyState
            glyph="shield"
            watermark
            title="Nothing waiting on you"
            description="Risky actions your bots attempt will queue here for a decision."
          />
        ) : null}

        {approvals.approvals.map((approval) => (
          <ApprovalCard
            key={approval.id}
            approval={approval}
            bot={botById[approval.bot_id]}
            deciding={Boolean(approvals.deciding[approval.id])}
            optimistic={approvals.optimistic[approval.id]}
            highlight={focusApprovalId === approval.id}
            onDecide={decide}
          />
        ))}
      </div>
    </section>
  )
}
