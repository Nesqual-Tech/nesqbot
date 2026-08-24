/**
 * What your bots are allowed to do without asking — and one click to stop it.
 *
 * ## Why this screen is the price of the feature
 *
 * A standing permission can be acquired *automatically*, after three identical
 * approvals, without anybody pressing a button that says "grant". That is what
 * the product owner asked for and it is the part worth being nervous about:
 * consent to three actions is not obviously consent to unlimited future ones,
 * and "the bot decided to stop asking" is a hard sentence to defend to an
 * auditor.
 *
 * Three things make it defensible, and this component is two of them. The first
 * is announcement — the reply of the turn a permission is acquired says so. The
 * second is that every permission is *listed*, with what it permits, when it was
 * granted, and on what evidence. The third is that any of them can be taken back
 * in one action. A feature that acquires authority quietly and gives it up
 * grudgingly is the one nobody should ship.
 *
 * ## Why the provenance is on the row and not behind a fold
 *
 * "You asked: don't ask again for this button" and "You approved this three
 * times running" are different grounds, and which one a bot acted on is the
 * whole question. Putting it one click away would make the common case —
 * skimming the list — the case where you cannot tell an automatic grant from a
 * deliberate one.
 */
import { useState } from "react"
import { errorMessage } from "../api/client"
import { cx, relativeTime } from "../lib/format"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"
import type { StandingApprovalsApi } from "../hooks/useStandingApprovals"
import type { Bot, StandingApproval } from "../types"

export interface StandingPermissionsProps {
  standing: StandingApprovalsApi
  bots: Bot[]
}

/** Where the permission came from, in one clause a person can check. */
function grounds(rule: StandingApproval): string {
  if (rule.origin === "note" && rule.note) return `You asked: “${rule.note}”`
  if (rule.origin === "repetition") {
    return `You approved this ${rule.source_approval_ids.length} times running`
  }
  return "Granted from an approval you made"
}

function Rule({
  rule,
  botName,
  revoking,
  onRevoke,
}: {
  rule: StandingApproval
  botName: string
  revoking: boolean
  onRevoke: () => void
}) {
  const revoked = Boolean(rule.revoked_at)
  return (
    <li className={cx("standing", revoked && "standing--revoked")} data-risk={rule.risk}>
      <div className="standing__main">
        <p className="standing__permits">{rule.permits}</p>
        <p className="standing__grounds">{grounds(rule)}</p>
        <p className="standing__meta">
          <span>{botName}</span>
          <span aria-hidden="true">·</span>
          <span>granted {relativeTime(rule.granted_at)}</span>
          <span aria-hidden="true">·</span>
          {/*
            Used-count, because "what did this actually do" is the second thing
            anybody wants after "what is it allowed to do". A permission that has
            never been used is a permission you can revoke without thinking.
          */}
          <span>{rule.used === 0 ? "never used" : `used ${rule.used}×`}</span>
          {rule.last_used_at ? (
            <>
              <span aria-hidden="true">·</span>
              <span>last {relativeTime(rule.last_used_at)}</span>
            </>
          ) : null}
        </p>
      </div>
      {revoked ? (
        <span className="chip chip--muted">revoked {relativeTime(rule.revoked_at!)}</span>
      ) : (
        <button type="button" className="btn btn--ghost btn--sm" onClick={onRevoke} disabled={revoking}>
          {revoking ? <Spinner inline label="Revoking" /> : "Stop allowing this"}
        </button>
      )}
    </li>
  )
}

export function StandingPermissions({ standing, bots }: StandingPermissionsProps) {
  const [failure, setFailure] = useState<string | null>(null)
  const [open, setOpen] = useState(false)

  const nameOf = (botId: string) => bots.find((bot) => bot.id === botId)?.name ?? "a bot"
  const live = standing.rules.filter((rule) => !rule.revoked_at).length

  const revoke = async (id: string) => {
    setFailure(null)
    try {
      await standing.revoke(id)
    } catch (err) {
      setFailure(errorMessage(err))
    }
  }

  return (
    <section className="standing-panel">
      <button
        type="button"
        className="disclosure standing-panel__toggle"
        aria-expanded={open}
        aria-controls="standing-permissions"
        onClick={() => setOpen((prev) => !prev)}
      >
        <Icon name={open ? "collapse" : "expand"} size={14} />
        {live === 0
          ? "Standing permissions — nothing is allowed without asking"
          : `Standing permissions — ${live} thing${live === 1 ? "" : "s"} I no longer ask about`}
      </button>

      {open ? (
        <div className="reveal" id="standing-permissions">
          {/*
            The limit, stated before the list rather than discovered later.

            The sentence comes from the server, because it is a promise about
            the gate and a copy of it in this file is a copy that can go stale.
          */}
          {standing.alwaysAsks ? (
            <p className="standing-panel__limit">
              <Icon name="shield" size={13} />
              <span>{standing.alwaysAsks}</span>
            </p>
          ) : null}

          {standing.error && standing.rules.length === 0 ? (
            <ErrorState
              error={standing.error}
              title="Standing permissions unavailable"
              onRetry={() => void standing.refetch()}
            />
          ) : null}

          {!standing.initialising && !standing.error && standing.rules.length === 0 ? (
            <EmptyState
              glyph="shield"
              title="Nothing is allowed without asking"
              description="If you tell a bot to stop asking about a particular button, or approve the same one three times running, it will appear here."
            />
          ) : null}

          {standing.rules.length > 0 ? (
            <ul className="standing-list">
              {standing.rules.map((rule) => (
                <Rule
                  key={rule.id}
                  rule={rule}
                  botName={nameOf(rule.bot_id)}
                  revoking={Boolean(standing.revoking[rule.id])}
                  onRevoke={() => void revoke(rule.id)}
                />
              ))}
            </ul>
          ) : null}

          <label className="standing-panel__history">
            <input
              type="checkbox"
              checked={standing.includeRevoked}
              onChange={(event) => standing.setIncludeRevoked(event.target.checked)}
            />
            {/*
              Revoked rules are kept, never deleted — "what was this bot allowed
              to do in March" has to stay answerable — so the history is a
              filter rather than a different screen.
            */}
            <span>Include ones I have already revoked</span>
          </label>

          {failure ? (
            <div className="inline-error" role="alert">
              {failure}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
