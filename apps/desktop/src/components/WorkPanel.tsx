/**
 * What the bots are working on — the second screen this app never had.
 *
 * `GET /work-items` and its five siblings shipped with the work-item lane and
 * nothing rendered any of them; `docs/STATUS.md` recorded the consequence in
 * four words: "/work-items still has none". The failure that made it matter,
 * reported by the person paying for it: a chief of staff answered a goal by
 * filing two rows —
 *
 *     create_work_item  "Lead Generator: Generate 20 qualified leads…"
 *     create_work_item  "Sales: Prepare to close deals…"
 *
 * — and there was nowhere to look at them. The work existed, the record
 * existed, and the only way to read either was `curl` with a bearer token.
 *
 * Two decisions worth stating, because both are about not lying:
 *
 * * **Progress is counted, never estimated.** The header counts items by
 *   status, which is a fact the API returns. There is no percentage bar: a
 *   lead that has been "working" for two days is not 60% done, and a bar would
 *   invent that number.
 * * **A hand-off is shown as the ledger row it is.** Expanding an item lists
 *   `work_item_transfers` — who moved it, to whom, why. That is the difference
 *   between "Sales has it" and "somebody wrote Sales in a title", which is
 *   exactly the confusion the filing-versus-delegating fix exists for.
 */
import { useCallback, useEffect, useState } from "react"
import { listWorkItemTransfers, listWorkItems } from "../api/endpoints"
import { prettyJson, relativeTime } from "../lib/format"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon, type IconName } from "./Icon"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import type { Bot, WorkItem, WorkItemStatus, WorkItemTransfer } from "../types"

const PAGE_SIZE = 100

/** The API's own vocabulary (`schemas.WorkItemStatus`), in the order a piece of
 *  work moves through it. Closed last because it is the only terminal one. */
const STATUS_ORDER: WorkItemStatus[] = ["working", "waiting", "open", "closed"]

const STATUS_LABELS: Record<WorkItemStatus, string> = {
  open: "Open",
  working: "Working",
  waiting: "Waiting",
  closed: "Closed",
}

/** Plain words for what each status means, because "waiting" is ambiguous
 *  until you know it means waiting on somebody else rather than queued. */
const STATUS_HINTS: Record<WorkItemStatus, string> = {
  open: "logged, nobody has picked it up",
  working: "a bot is on it now",
  waiting: "blocked on somebody else — a reply, an approval, another bot",
  closed: "finished, with a resolution",
}

function iconFor(type: string): IconName {
  const kind = String(type)
  if (kind === "lead") return "search"
  if (kind === "deal") return "chart"
  if (kind === "ticket") return "shield"
  if (kind === "task") return "list"
  return "blocks"
}

interface WorkRowProps {
  item: WorkItem
  botName: (botId?: string | null) => string | null
}

function WorkRow({ item, botName }: WorkRowProps) {
  const [open, setOpen] = useState(false)
  const [transfers, setTransfers] = useState<WorkItemTransfer[] | null>(null)
  const [loadingTransfers, setLoadingTransfers] = useState(false)
  const owner = botName(item.owner_bot_id)
  const hasDetail = item.detail && Object.keys(item.detail).length > 0

  /*
   * The ledger is fetched on expand rather than with the list. A hundred items
   * would otherwise be a hundred extra requests to render a screen where most
   * rows are never opened, and the list endpoint does not carry transfers.
   */
  const expand = async () => {
    const next = !open
    setOpen(next)
    if (!next || transfers !== null || loadingTransfers) return
    setLoadingTransfers(true)
    try {
      setTransfers(await listWorkItemTransfers(item.id))
    } catch {
      // A ledger that will not load must not take the row with it — the item's
      // own fields are already on screen and still true.
      setTransfers([])
    } finally {
      setLoadingTransfers(false)
    }
  }

  return (
    <li className="audit-row">
      <button type="button" className="audit-row__summary" onClick={expand} aria-expanded={open}>
        <Icon name={iconFor(item.type)} size={16} className="audit-row__icon" />
        <span className="audit-row__type">{item.title}</span>
        <span
          className={`state-pill${item.status === "working" ? " state-pill--ok" : item.status === "waiting" ? " state-pill--warn" : ""}`}
          title={STATUS_HINTS[item.status]}
        >
          {STATUS_LABELS[item.status] ?? item.status}
        </span>
        {owner ? <span className="audit-row__bot">{owner}</span> : null}
        {/*
          Assigned and not yet started. `dispatched_at: null` with an owner and
          `status: open` is the API dispatcher's queue — the bot is seconds from
          getting its own run on this — and saying so is the difference between
          a list that looks inert and one you can watch move.
        */}
        {item.status === "open" && item.owner_bot_id && !item.dispatched_at ? (
          <span className="chip chip--ok" title="Assigned. Its bot is being started on it.">
            starting
          </span>
        ) : null}
        <span className="audit-row__time" title={item.updated_at}>
          {relativeTime(item.updated_at)}
        </span>
        <Icon name={open ? "collapse" : "expand"} size={13} className="audit-row__chevron" />
      </button>

      {open ? (
        <div className="audit-row__detail">
          {item.summary ? <p>{item.summary}</p> : null}
          {item.resolution ? <p>Resolution: {item.resolution}</p> : null}
          <p>
            Logged {relativeTime(item.created_at)}
            {item.transferred_at ? `, last handed over ${relativeTime(item.transferred_at)}` : ""}
            {item.last_event_at ? `, last outside contact ${relativeTime(item.last_event_at)}` : ""}
            {item.closed_at ? `, closed ${relativeTime(item.closed_at)}` : ""}
          </p>

          {loadingTransfers ? <Spinner /> : null}
          {transfers && transfers.length > 0 ? (
            <ol className="audit-list">
              {transfers.map((transfer) => (
                <li key={transfer.id}>
                  {botName(transfer.from_bot_id) ?? "logged"} → {botName(transfer.to_bot_id) ?? "another bot"}
                  {transfer.reason ? `: ${transfer.reason}` : ""}{" "}
                  <span className="audit-row__time" title={transfer.created_at}>
                    {relativeTime(transfer.created_at)}
                  </span>
                </li>
              ))}
            </ol>
          ) : null}
          {transfers && transfers.length === 0 && !loadingTransfers ? (
            <p>No hand-offs yet — this has stayed with the bot that logged it.</p>
          ) : null}

          {hasDetail ? <pre>{prettyJson(item.detail)}</pre> : null}
        </div>
      ) : null}
    </li>
  )
}

export function WorkPanel({ bots }: { bots: Bot[] }) {
  const [items, setItems] = useState<WorkItem[]>([])
  const [statusFilter, setStatusFilter] = useState<string>("")
  const [botFilter, setBotFilter] = useState<string>("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const botName = useCallback(
    (botId?: string | null) => (botId ? (bots.find((b) => b.id === botId)?.name ?? null) : null),
    [bots],
  )

  const load = useCallback(async () => {
    const query: { status?: string; owner_bot_id?: string; limit: number } = { limit: PAGE_SIZE }
    if (statusFilter) query.status = statusFilter
    if (botFilter) query.owner_bot_id = botFilter
    setItems(await listWorkItems(query))
  }, [statusFilter, botFilter])

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
    // `load` changes identity with both filters, which is what this effect re-runs on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, botFilter])

  /*
   * Counted from the rows on screen, and only when no status filter is on.
   * A tally that silently described a filtered subset as the whole picture
   * would be worse than no tally: "3 working" has to mean three, not three of
   * the ones you happen to be looking at.
   */
  const counts = STATUS_ORDER.map((status) => ({
    status,
    total: items.filter((item) => item.status === status).length,
  })).filter((entry) => entry.total > 0)

  return (
    <section className="panel" id="panel-work" role="tabpanel" aria-labelledby="nav-tab-work">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Delivery</div>
          <h2 className="panel__title">Work</h2>
          <p className="panel__subtitle">
            Every lead, deal and task your bots have written down, newest first. Expand one to see
            who has been handed it and why.
          </p>
          {!statusFilter && counts.length > 0 ? (
            <p className="panel__subtitle">
              {counts.map((entry, index) => (
                <span key={entry.status} title={STATUS_HINTS[entry.status]}>
                  {index > 0 ? " · " : ""}
                  {entry.total} {STATUS_LABELS[entry.status].toLowerCase()}
                </span>
              ))}
            </p>
          ) : null}
        </div>
        <div className="panel__header-actions">
          <label className="sr-only" htmlFor="work-status-filter">
            Filter by status
          </label>
          <select
            id="work-status-filter"
            className="select"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="">Any status</option>
            {STATUS_ORDER.map((status) => (
              <option key={status} value={status}>
                {STATUS_LABELS[status]}
              </option>
            ))}
          </select>
          <label className="sr-only" htmlFor="work-bot-filter">
            Filter by bot
          </label>
          <select
            id="work-bot-filter"
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

        {error && items.length === 0 && !loading ? (
          <ErrorState error={error} title="Work items unavailable" onRetry={() => void load()} />
        ) : null}

        {!loading && !error && items.length === 0 ? (
          <EmptyState
            glyph="blocks"
            title={statusFilter || botFilter ? "Nothing matches those filters" : "Nothing logged yet"}
            description="When a bot logs a lead, a deal or a task, it lands here — with the hand-offs that moved it."
          />
        ) : null}

        {items.length > 0 ? (
          <ul className="audit-list">
            {items.map((item) => (
              <WorkRow key={item.id} item={item} botName={botName} />
            ))}
          </ul>
        ) : null}
      </div>
    </section>
  )
}
