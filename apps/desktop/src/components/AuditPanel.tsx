/**
 * Who did what, when — the one screen this app never had.
 *
 * `GET /audit` has existed since the contract's first version and every
 * client function for it (`listAudit`) has too; nothing rendered it. An
 * approval decision, a budget change, a connector binding — all of it was
 * genuinely recorded and genuinely invisible, reachable only with `curl` and
 * a bearer token. For a product whose whole pitch is "a human is in the
 * loop on anything consequential", the record of who was in that loop
 * belonged on screen, not just in the database.
 */
import { useCallback, useEffect, useState } from "react"
import { listAudit } from "../api/endpoints"
import { prettyJson, relativeTime } from "../lib/format"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon, type IconName } from "./Icon"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import type { AuditEvent, AuditEventType, Bot } from "../types"

const PAGE_SIZE = 50

/**
 * One glyph per rough category, read off the event's own prefix
 * (`approval_*`, `bot_*`, …) rather than a 50-entry lookup table — a new
 * event type this file has never heard of still lands on a sensible icon
 * instead of a blank one, which a fixed table cannot promise.
 */
function iconFor(eventType: AuditEventType): IconName {
  const type = String(eventType)
  if (type.startsWith("approval") || type.startsWith("standing_approval")) return "shield"
  if (type.startsWith("bot_delegation")) return "repeat"
  if (type.startsWith("desktop")) return "monitor"
  if (type.startsWith("connector") || type.startsWith("mcp")) return "plug"
  if (type.startsWith("routine")) return "repeat"
  if (type.startsWith("budget")) return "chart"
  if (type.startsWith("run") || type === "chat_turn") return "chat"
  if (type.startsWith("work_item")) return "blocks"
  if (type.startsWith("inbound")) return "search"
  if (type.startsWith("kb_article")) return "copy"
  if (type === "human_takeover_requested") return "user"
  return "list"
}

/** `approval_decision` -> "approval decision". Good enough without a
 * hand-maintained label for every one of the ~45 event types the API emits. */
function humanizeEventType(eventType: AuditEventType): string {
  return String(eventType).replace(/_/g, " ")
}

interface AuditRowProps {
  event: AuditEvent
  botName: (botId?: string | null) => string | null
}

function AuditRow({ event, botName }: AuditRowProps) {
  const [open, setOpen] = useState(false)
  const bot = botName(event.bot_id)
  const hasDetail = event.detail && Object.keys(event.detail).length > 0

  return (
    <li className="audit-row">
      <button
        type="button"
        className="audit-row__summary"
        onClick={() => hasDetail && setOpen((prev) => !prev)}
        aria-expanded={hasDetail ? open : undefined}
        disabled={!hasDetail}
      >
        <Icon name={iconFor(event.event_type)} size={16} className="audit-row__icon" />
        <span className="audit-row__type">{humanizeEventType(event.event_type)}</span>
        {bot ? <span className="audit-row__bot">{bot}</span> : null}
        <span className="audit-row__time" title={event.created_at}>
          {relativeTime(event.created_at)}
        </span>
        {hasDetail ? <Icon name={open ? "collapse" : "expand"} size={13} className="audit-row__chevron" /> : null}
      </button>
      {open && hasDetail ? (
        <pre className="audit-row__detail">{prettyJson(event.detail)}</pre>
      ) : null}
    </li>
  )
}

export function AuditPanel({ bots }: { bots: Bot[] }) {
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [botFilter, setBotFilter] = useState<string>("")
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [exhausted, setExhausted] = useState(false)

  const botName = useCallback(
    (botId?: string | null) => (botId ? (bots.find((b) => b.id === botId)?.name ?? null) : null),
    [bots],
  )

  const load = useCallback(
    async (before?: string) => {
      const query: { bot_id?: string; limit: number; before?: string } = { limit: PAGE_SIZE }
      if (botFilter) query.bot_id = botFilter
      if (before) query.before = before
      const page = await listAudit(query)
      setExhausted(page.length < PAGE_SIZE)
      setEvents((current) => (before ? [...current, ...page] : page))
    },
    [botFilter],
  )

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    load()
      .catch((err) => !cancelled && setError(err))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
    // `load` changes identity with `botFilter`, which is exactly the dependency this effect exists to re-run on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botFilter])

  const loadMore = async () => {
    const last = events[events.length - 1]
    if (!last) return
    setLoadingMore(true)
    try {
      await load(last.created_at)
    } catch (err) {
      setError(err)
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <section className="panel" id="panel-audit" role="tabpanel" aria-labelledby="nav-tab-audit">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Trust</div>
          <h2 className="panel__title">Audit</h2>
          <p className="panel__subtitle">Every gated decision and system event, newest first. Nothing here is edited or deleted.</p>
        </div>
        <div className="panel__header-actions">
          <label className="sr-only" htmlFor="audit-bot-filter">
            Filter by bot
          </label>
          <select
            id="audit-bot-filter"
            className="select"
            value={botFilter}
            onChange={(event) => setBotFilter(event.target.value)}
          >
            <option value="">All bots</option>
            {bots.map((bot) => (
              <option key={bot.id} value={bot.id}>
                {bot.name}
              </option>
            ))}
          </select>
        </div>
      </header>

      <div className="panel__body">
        {loading ? <SkeletonCards cards={3} /> : null}

        {error && events.length === 0 && !loading ? (
          <ErrorState error={error} title="Audit trail unavailable" onRetry={() => void load()} />
        ) : null}

        {!loading && !error && events.length === 0 ? (
          <EmptyState glyph="list" title="Nothing recorded yet" description="Gated actions, approvals and system events will appear here as they happen." />
        ) : null}

        {events.length > 0 ? (
          <>
            <ul className="audit-list">
              {events.map((event) => (
                <AuditRow key={event.id} event={event} botName={botName} />
              ))}
            </ul>
            {!exhausted ? (
              <button type="button" className="btn btn--ghost btn--sm audit-load-more" onClick={() => void loadMore()} disabled={loadingMore}>
                {loadingMore ? <Spinner inline label="Loading" /> : "Load more"}
              </button>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  )
}
