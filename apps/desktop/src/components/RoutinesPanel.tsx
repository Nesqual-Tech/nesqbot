import { useState } from "react"
import { errorMessage } from "../api/client"
import { useRoutines } from "../hooks/useRoutines"
import { cx, describeCron, isPlausibleCron, plural, prettyJson, relativeTime } from "../lib/format"
import { useToast } from "../state/AppState"
import { EmptyState, ErrorState } from "./EmptyState"
import { Markdown } from "./Markdown"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import type { Bot } from "../types"

export interface RoutinesPanelProps {
  bots: Bot[]
  activeBotId: string | null
  onSelectBot: (botId: string | null) => void
}

export function RoutinesPanel({ bots, activeBotId, onSelectBot }: RoutinesPanelProps) {
  const toast = useToast()
  const [scope, setScope] = useState<"all" | "bot">("all")
  const routines = useRoutines(scope === "bot" ? activeBotId : null)
  const [cronDrafts, setCronDrafts] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  const botName = (botId: string) => bots.find((b) => b.id === botId)?.name ?? botId.slice(0, 8)

  const withBusy = async (id: string, fn: () => Promise<unknown>, okMessage?: string) => {
    setBusyId(id)
    try {
      await fn()
      if (okMessage) toast.success(okMessage)
    } catch (err) {
      toast.error("Routine request failed", errorMessage(err))
    } finally {
      setBusyId(null)
    }
  }

  const toggleRuns = async (id: string) => {
    const next = !expanded[id]
    setExpanded((prev) => ({ ...prev, [id]: next }))
    if (next && !routines.runs[id]) await routines.loadRuns(id)
  }

  return (
    <section className="panel" id="panel-routines" role="tabpanel" aria-labelledby="nav-tab-routines">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Automation</div>
          <h2 className="panel__title">Routines</h2>
          <p className="panel__subtitle">
            Taught from real desktop demonstrations. Record new ones on a teammate's Agent Computer.
          </p>
        </div>
        <div className="panel__header-actions">
          <label className="sr-only" htmlFor="routine-scope">
            Scope
          </label>
          <select
            id="routine-scope"
            className="select"
            value={scope}
            onChange={(event) => setScope(event.target.value === "bot" ? "bot" : "all")}
          >
            <option value="all">All bots</option>
            <option value="bot">Selected bot only</option>
          </select>
          {scope === "bot" ? (
            <select
              className="select"
              aria-label="Bot filter"
              value={activeBotId ?? ""}
              onChange={(event) => onSelectBot(event.target.value || null)}
            >
              <option value="">Select a bot…</option>
              {bots.map((bot) => (
                <option key={bot.id} value={bot.id}>
                  {bot.name}
                </option>
              ))}
            </select>
          ) : null}
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void routines.refetch()}
            disabled={routines.loading}
          >
            {routines.loading ? <Spinner inline label="Refreshing" /> : "Refresh"}
          </button>
        </div>
      </header>

      <div className="panel__body panel__body--grid">
        {routines.initialising ? <SkeletonCards cards={2} /> : null}

        {routines.error && routines.routines.length === 0 && !routines.initialising ? (
          <ErrorState error={routines.error} title="Routines unavailable" onRetry={() => void routines.refetch()} />
        ) : null}

        {!routines.initialising && !routines.error && routines.routines.length === 0 ? (
          <EmptyState
            glyph="repeat"
            title="No routines yet"
            description="Open a teammate's computer, hit Record, do the job once, then save it as a routine."
          />
        ) : null}

        {routines.routines.map((routine) => {
          const cronDraft = cronDrafts[routine.id] ?? routine.schedule_cron ?? ""
          const cronValid = isPlausibleCron(cronDraft)
          const runsState = routines.runs[routine.id]
          return (
            <article className="card routine" key={routine.id}>
              <header className="connector__header">
                <div>
                  <h3 className="card__title">{routine.name}</h3>
                  <p className="card__body">
                    {botName(routine.bot_id)} · v{routine.version} · {plural(routine.steps?.length ?? 0, "step")}
                    {/*
                      The schedule in English where it can be, and the raw
                      expression only where it cannot. "cron 0 7 * * 1-5" is
                      unreadable to exactly the person this feature is sold to;
                      see `describeCron` for why it declines to guess at the
                      shapes it does not understand.
                    */}
                    {" · "}
                    {routine.schedule_cron ? (
                      <span title={routine.schedule_cron}>
                        {describeCron(routine.schedule_cron) ?? `cron ${routine.schedule_cron}`}
                      </span>
                    ) : (
                      "on demand"
                    )}
                  </p>
                  {/* Model-written, so Markdown — inline only, it is one line
                      under a card title. */}
                  {routine.description ? (
                    <p className="muted">
                      <Markdown text={routine.description} inline />
                    </p>
                  ) : null}
                </div>
                <label className="switch">
                  <input
                    type="checkbox"
                    checked={routine.enabled}
                    disabled={busyId === routine.id}
                    onChange={(event) =>
                      void withBusy(
                        routine.id,
                        () => routines.update(routine.id, { enabled: event.target.checked }),
                        event.target.checked ? "Routine enabled" : "Routine disabled",
                      )
                    }
                  />
                  <span>{routine.enabled ? "enabled" : "disabled"}</span>
                </label>
              </header>

              <label className="field">
                <span className="field__label">Schedule (cron)</span>
                <div className="field__row">
                  <input
                    className={cx("input", !cronValid && "input--invalid")}
                    value={cronDraft}
                    aria-invalid={!cronValid}
                    placeholder="0 9 * * 1"
                    onChange={(event) => setCronDrafts((prev) => ({ ...prev, [routine.id]: event.target.value }))}
                  />
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    disabled={!cronValid || busyId === routine.id}
                    onClick={() =>
                      void withBusy(
                        routine.id,
                        () => routines.update(routine.id, { schedule_cron: cronDraft.trim() || null }),
                        "Schedule saved",
                      )
                    }
                  >
                    Save
                  </button>
                </div>
              </label>

              <div className="row-actions">
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  disabled={Boolean(routines.running[routine.id])}
                  onClick={() =>
                    void withBusy(routine.id, async () => {
                      const started = await routines.runNow(routine.id)
                      toast.success("Routine started", started.workflow_id ?? "queued")
                    })
                  }
                >
                  {routines.running[routine.id] ? <Spinner inline label="Starting" /> : "Run now"}
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  aria-expanded={Boolean(expanded[routine.id])}
                  onClick={() => void toggleRuns(routine.id)}
                >
                  {expanded[routine.id] ? "Hide runs" : "Recent runs"}
                </button>
                {/*
                  The last native `confirm()` in the product, replaced.

                  A packaged desktop app that answers "delete this routine?"
                  with an unstyled browser dialog saying "localhost:1420 says"
                  is telling on itself. The consequence is stated where the
                  routine is, in the same shape the approval card and the Bot
                  Desktop wipe control now use.
                */}
                {confirmDelete === routine.id ? (
                  <span className="danger-confirm" role="alert">
                    <span className="danger-confirm__text">Removes {routine.name} and its schedule.</span>
                    <button
                      type="button"
                      className="btn btn--quiet-danger btn--sm"
                      disabled={busyId === routine.id}
                      onClick={() => {
                        setConfirmDelete(null)
                        void withBusy(routine.id, () => routines.remove(routine.id), "Routine deleted")
                      }}
                    >
                      Delete it
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost btn--sm"
                      autoFocus
                      onClick={() => setConfirmDelete(null)}
                    >
                      Keep
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm routine__delete"
                    disabled={busyId === routine.id}
                    onClick={() => setConfirmDelete(routine.id)}
                  >
                    Delete routine
                  </button>
                )}
              </div>

              {expanded[routine.id] ? (
                <div className="routine__runs">
                  {runsState?.loading ? <Spinner inline label="Loading runs" /> : null}
                  {runsState?.error ? <div className="inline-error">{runsState.error}</div> : null}
                  {runsState && !runsState.loading && runsState.runs.length === 0 ? (
                    <p className="muted">No runs recorded yet.</p>
                  ) : null}
                  {runsState?.runs.length ? (
                    <table className="table">
                      <thead>
                        <tr>
                          <th scope="col">Status</th>
                          <th scope="col">Started</th>
                          <th scope="col">Workflow</th>
                        </tr>
                      </thead>
                      <tbody>
                        {runsState.runs.map((run) => (
                          <tr key={run.id}>
                            <td>
                              <span
                                className={cx(
                                  "chip",
                                  run.status === "completed"
                                    ? "chip--ok"
                                    : run.status === "failed"
                                      ? "chip--error"
                                      : "chip--warn",
                                )}
                                title={run.error ?? undefined}
                              >
                                {run.status}
                              </span>
                            </td>
                            <td>{relativeTime(run.created_at ?? null)}</td>
                            <td>
                              <code>{(run.temporal_workflow_id ?? "—").slice(0, 24)}</code>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : null}
                  <details className="routine__steps">
                    <summary>Steps</summary>
                    <pre className="code-block code-block--scroll">{prettyJson(routine.steps ?? [])}</pre>
                  </details>
                </div>
              ) : null}
            </article>
          )
        })}
      </div>
    </section>
  )
}
