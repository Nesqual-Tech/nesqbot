import { useCallback, useEffect, useState, type CSSProperties } from "react"
import { getBotColor, riskLabels } from "@nesqbot/ui"
import { errorMessage } from "../api/client"
import { createMemory, deleteMemory, getProviders, listMemories, reseedSystemBots } from "../api/endpoints"
import type { BotsApi } from "../hooks/useBots"
import { cx, initials, relativeTime, usd } from "../lib/format"
import { GATED_RISKS } from "../lib/risk"
import { useToast } from "../state/AppState"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"
import type { Bot, DesktopProfile, Memory, MemoryKind, ModelProvider } from "../types"

const MEMORY_KINDS: MemoryKind[] = ["fact", "preference", "contact", "procedure", "note"]

/**
 * What this one bot has learned — separate from the Knowledge tab's shared
 * KB on purpose: `GET/POST /bots/{id}/memories` is scoped to a bot (and
 * within it, per user), where `/kb` is organisation-wide. Living inside the
 * edit form rather than its own tab because there is nothing to say about a
 * bot's memories without a bot already selected — the same reasoning that
 * keeps the provider/model picker here instead of a dedicated screen.
 */
function MemoriesSection({ bot }: { bot: Bot }) {
  const toast = useToast()
  const [memories, setMemories] = useState<Memory[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [kind, setKind] = useState<MemoryKind>("note")
  const [content, setContent] = useState("")
  const [adding, setAdding] = useState(false)
  const [open, setOpen] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      setMemories(await listMemories(bot.id, 50))
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [bot.id])

  useEffect(() => {
    setLoading(true)
    void load()
  }, [load])

  const add = async () => {
    if (!content.trim()) return
    setAdding(true)
    try {
      await createMemory(bot.id, { kind, content: content.trim() })
      setContent("")
      toast.success("Memory added")
      await load()
    } catch (err) {
      toast.error("Could not add the memory", errorMessage(err))
    } finally {
      setAdding(false)
    }
  }

  const remove = async (id: string) => {
    try {
      await deleteMemory(id)
      setMemories((current) => current.filter((m) => m.id !== id))
    } catch (err) {
      toast.error("Could not remove the memory", errorMessage(err))
    }
  }

  return (
    <section className="card builder__memories">
      <button
        type="button"
        className="disclosure"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        <Icon name={open ? "collapse" : "expand"} size={14} />
        {open ? "Hide memories" : `What ${bot.name} remembers${loading ? "" : ` (${memories.length})`}`}
      </button>

      {open ? (
        <div className="reveal builder__memories-body">
          <div className="builder__memory-add">
            <select className="select" value={kind} onChange={(event) => setKind(event.target.value as MemoryKind)}>
              {MEMORY_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <input
              className="input"
              value={content}
              onChange={(event) => setContent(event.target.value)}
              placeholder="Something this bot should remember…"
              onKeyDown={(event) => event.key === "Enter" && void add()}
            />
            <button type="button" className="btn btn--primary btn--sm" onClick={() => void add()} disabled={adding || !content.trim()}>
              {adding ? <Spinner inline label="Adding" /> : "Add"}
            </button>
          </div>

          {loading ? <Spinner inline label="Loading memories" /> : null}
          {error && !loading ? <ErrorState error={error} title="Memories unavailable" onRetry={() => void load()} /> : null}
          {!loading && !error && memories.length === 0 ? (
            <p className="field__hint">Nothing yet. Memories accumulate as this bot works, or add one by hand above.</p>
          ) : null}

          {memories.length > 0 ? (
            <ul className="builder__memory-list">
              {memories.map((memory) => (
                <li key={memory.id} className="builder__memory-item">
                  <span className="chip chip--muted">{memory.kind}</span>
                  <span className="builder__memory-content">{memory.content}</span>
                  <span className="builder__memory-time">{relativeTime(memory.created_at)}</span>
                  <button
                    type="button"
                    className="btn btn--ghost btn--xs"
                    onClick={() => void remove(memory.id)}
                    aria-label="Delete this memory"
                  >
                    <Icon name="trash" size={13} />
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

/**
 * Starting points for "New teammate" — a role, a prompt and a desktop
 * profile someone can start from and rename, not a bot they're stuck with.
 * Written to the same rule `docs/bots.md` holds the five system bots to:
 * describe the *role* only. The desktop capability text, the action
 * protocol and the approval rules are composed in at turn time by
 * `services.orchestrator` from one constant — repeating them here would be
 * the sixth copy of the same paragraph the system-bot YAML files already
 * agreed not to have.
 */
interface BotTemplate {
  id: string
  label: string
  role: string
  desktop_profile: DesktopProfile
  system_prompt: string
}

const BOT_TEMPLATES: BotTemplate[] = [
  {
    id: "support",
    label: "Customer Support",
    role: "Tickets & KB",
    desktop_profile: "xfce",
    system_prompt:
      "You classify incoming tickets, draft KB-grounded replies with citations, and escalate with a context " +
      "pack when the knowledge base does not answer something — say so plainly rather than inventing a " +
      "procedure. Do the classification and the drafting in this turn rather than describing them. Sending is " +
      "gated and stops for a human on its own.",
  },
  {
    id: "research",
    label: "Research Analyst",
    role: "Market & company research",
    desktop_profile: "xfce",
    system_prompt:
      "You research companies, markets and competitors, and turn what you find into a structured brief with " +
      "sources. You have a real Linux desktop with a browser: open the site, read what is actually there, and " +
      "record what you actually found — never a fact you did not see. Do the research in this turn rather than " +
      "describing how you would approach it.",
  },
  {
    id: "writer",
    label: "Content Writer",
    role: "Drafts & copy",
    desktop_profile: "icewm",
    system_prompt:
      "You draft blog posts, social copy and marketing pages in the voice you are given, and revise against " +
      "feedback rather than re-explaining the brief back. Write the draft in this turn; do not describe what " +
      "you are about to write. Publishing anywhere external is gated and stops for a human.",
  },
  {
    id: "assistant",
    label: "Executive Assistant",
    role: "Calendar & inbox",
    desktop_profile: "xfce",
    system_prompt:
      "You triage the inbox, manage the calendar, and prepare briefs before meetings. Flag conflicts and " +
      "overdue replies rather than letting them sit. Do the triage in this turn rather than listing what you " +
      "could do. Sending and scheduling on someone else's behalf are gated and stop for a human.",
  },
  {
    id: "recruiter",
    label: "Recruiter",
    role: "Sourcing & screening",
    desktop_profile: "xfce",
    system_prompt:
      "You source candidates, screen resumes against a role's requirements, and draft outreach. Score fit " +
      "against the stated requirements, not a vibe, and say when a candidate is a stretch rather than " +
      "inflating the match. Do the sourcing and screening in this turn. Sending outreach is gated and stops " +
      "for a human.",
  },
  {
    id: "analyst",
    label: "Data Analyst",
    role: "Reports & dashboards",
    desktop_profile: "icewm",
    system_prompt:
      "You pull numbers into structured reports, flag anomalies against the prior period, and keep a running " +
      "note of where each figure came from. A total that does not reconcile gets flagged, never smoothed over. " +
      "Do the analysis in this turn rather than describing the approach.",
  },
]

const PROVIDER_LABEL: Record<ModelProvider, string> = {
  azure: "Azure OpenAI",
  openai: "OpenAI / local model",
  anthropic: "Anthropic",
  google: "Google",
}

/** Which providers the backend can actually reach — same source `SetupWizard` reads, cached for this panel's lifetime. */
function useAvailableProviders(): ModelProvider[] {
  const [providers, setProviders] = useState<ModelProvider[]>([])
  useEffect(() => {
    const controller = new AbortController()
    getProviders(controller.signal)
      .then((result) => setProviders((Object.keys(result) as ModelProvider[]).filter((key) => result[key])))
      .catch(() => undefined)
    return () => controller.abort()
  }, [])
  return providers
}

export interface BuilderPanelProps {
  bots: BotsApi
  activeBotId: string | null
  onSelectBot: (botId: string) => void
}

const PROFILES: DesktopProfile[] = ["xfce", "icewm"]

const PROFILE_NOTES: Record<string, string> = {
  xfce: "Full desktop. Heavier, and the one to pick if the bot has to drive a real browser.",
  icewm: "Minimal window manager. Starts faster and uses less memory.",
}

const EMPTY_DRAFT = {
  name: "",
  role: "",
  system_prompt: "",
  desktop_profile: "xfce" as DesktopProfile,
  daily_budget_usd: "5",
}

/* ------------------------------------------------------------------ *
 * The preview
 * ------------------------------------------------------------------ */

/**
 * The teammate, as it will exist.
 *
 * This panel is where somebody creates a colleague, and it used to render that
 * as two stacked CRUD forms — four inputs, a textarea, a Create button, and no
 * indication anywhere on the screen of what the thing being created *is* or
 * what it will be allowed to do. Everything the product argues about itself
 * (an isolated Linux desktop each, a hard daily cap, and a bot that stops
 * before anything consequential) was absent from the one screen where a person
 * decides to make one.
 *
 * None of that needs an API field. The desktop profile, the cap and the six
 * risk classes are all already in the form or already in the design system, so
 * the preview is a straight rendering of facts the page already holds — the
 * sidebar row it will become, and the governance rule it will be born under.
 */
function TeammatePreview({
  name,
  role,
  slug,
  profile,
  budget,
  isSystem,
  eyebrow,
}: {
  name: string
  role: string
  slug: string
  profile: string
  budget: number | null
  isSystem?: boolean
  eyebrow: string
}) {
  const displayName = name.trim() || "Your new teammate"
  const displayRole = role.trim() || (isSystem ? "System bot" : "Give it a job title")
  const unnamed = !name.trim()

  return (
    <aside className={cx("teammate-preview", unnamed && "teammate-preview--placeholder")} aria-label="Preview">
      <div className="teammate-preview__eyebrow">{eyebrow}</div>

      <div className="teammate-preview__identity">
        <span
          className="avatar avatar--lg"
          style={{ "--avatar-bg": getBotColor(slug) } as CSSProperties}
          aria-hidden="true"
        >
          {unnamed ? "?" : initials(displayName)}
        </span>
        <span className="teammate-preview__meta">
          <span className="teammate-preview__name">{displayName}</span>
          <span className="teammate-preview__role">{displayRole}</span>
        </span>
      </div>

      <dl className="teammate-preview__facts">
        <div>
          <dt>Its own machine</dt>
          <dd>
            An isolated Linux desktop · <code>{profile}</code>
          </dd>
        </div>
        <div>
          <dt>Daily cap</dt>
          <dd>{budget !== null && budget > 0 ? `${usd(budget)} a day, then it stops` : "No cap — it can spend freely"}</dd>
        </div>
      </dl>

      {/*
        The governance rule, stated at the moment of creation rather than only
        at the moment of interruption. It is fixed policy, not a setting, which
        is exactly why it is worth saying here: nobody has to configure safety.
      */}
      <div className="teammate-preview__policy">
        <p className="teammate-preview__policy-row" data-tone="free">
          <Icon name="check" size={13} />
          <span>
            <strong>Works on its own</strong> when reading, drafting or changing internal records.
          </span>
        </p>
        <p className="teammate-preview__policy-row" data-tone="gated">
          <Icon name="shield" size={13} />
          <span>
            <strong>Stops and asks you</strong> before it{" "}
            {GATED_RISKS.map((risk, index) => (
              <span key={risk}>
                {index > 0 ? (index === GATED_RISKS.length - 1 ? " or " : ", ") : ""}
                <span className="teammate-preview__risk" data-risk={risk}>
                  {(riskLabels[risk] ?? risk).toLowerCase()}s
                </span>
              </span>
            ))}
            .
          </span>
        </p>
      </div>
    </aside>
  )
}

/* ------------------------------------------------------------------ *
 * The panel
 * ------------------------------------------------------------------ */

export function BuilderPanel({ bots, activeBotId, onSelectBot }: BuilderPanelProps) {
  const toast = useToast()
  const [draft, setDraft] = useState(EMPTY_DRAFT)
  const [creating, setCreating] = useState(false)
  const [appliedTemplate, setAppliedTemplate] = useState<string | null>(null)
  const [reseeding, setReseeding] = useState(false)

  /** Fills role/prompt/profile from a template; the name stays whatever was
   * already typed — a template is a starting point, not a rename. */
  const applyTemplate = (template: BotTemplate) => {
    setAppliedTemplate(template.id)
    setDraft((current) => ({
      ...current,
      role: template.role,
      system_prompt: template.system_prompt,
      desktop_profile: template.desktop_profile,
    }))
  }

  const reseed = async () => {
    setReseeding(true)
    try {
      const result = await reseedSystemBots()
      toast.success("System bots reseeded", result.detail ?? "Done")
      await bots.refetch()
    } catch (err) {
      toast.error("Could not reseed", errorMessage(err))
    } finally {
      setReseeding(false)
    }
  }

  const selected: Bot | null = bots.bots.find((b) => b.id === activeBotId) ?? null
  const [edit, setEdit] = useState({
    name: "",
    role: "",
    system_prompt: "",
    daily_budget_usd: "",
    model_provider: "" as ModelProvider | "",
    model_name: "",
  })
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const availableProviders = useAvailableProviders()

  useEffect(() => {
    if (!selected) return
    setConfirmDelete(false)
    setEdit({
      name: selected.name,
      role: selected.role,
      system_prompt: selected.system_prompt ?? "",
      daily_budget_usd: String(selected.daily_budget_usd ?? ""),
      model_provider: selected.model_provider ?? "",
      model_name: selected.model_name ?? "",
    })
  }, [
    selected?.id,
    selected?.name,
    selected?.role,
    selected?.system_prompt,
    selected?.daily_budget_usd,
    selected?.model_provider,
    selected?.model_name,
  ])

  /*
   * What is missing, said before the button is pressed.
   *
   * Create used to be permanently enabled and answer an incomplete form with a
   * toast — an interruption in the corner of the screen telling you about a
   * field two inches from your cursor. The requirement is stated inline
   * instead, and the button still works, because a disabled control with no
   * explanation is the other half of the same mistake.
   */
  const draftBudget = Number(draft.daily_budget_usd)
  const missing: string[] = []
  if (!draft.name.trim()) missing.push("a name")
  if (!draft.system_prompt.trim()) missing.push("a system prompt")
  const budgetInvalid = draft.daily_budget_usd.trim() !== "" && !(Number.isFinite(draftBudget) && draftBudget >= 0)

  const create = async () => {
    if (missing.length > 0) {
      toast.warning("Not enough to build a teammate", `Still needs ${missing.join(" and ")}.`)
      return
    }
    setCreating(true)
    try {
      const bot = await bots.createBot({
        name: draft.name.trim(),
        role: draft.role.trim(),
        system_prompt: draft.system_prompt.trim(),
        desktop_profile: draft.desktop_profile,
        daily_budget_usd: Number.isFinite(draftBudget) && draftBudget > 0 ? draftBudget : undefined,
      })
      toast.success("Teammate created", `${bot.name} is on the team.`)
      setDraft(EMPTY_DRAFT)
      onSelectBot(bot.id)
    } catch (err) {
      toast.error("Could not create the bot", errorMessage(err))
    } finally {
      setCreating(false)
    }
  }

  const modelOverrideInvalid = Boolean(edit.model_provider) && !edit.model_name.trim()

  const save = async () => {
    if (!selected) return
    if (modelOverrideInvalid) {
      toast.warning("Model name required", "Pick a provider and a model together, or clear both.")
      return
    }
    const budget = Number(edit.daily_budget_usd)
    setSaving(true)
    try {
      await bots.updateBot(selected.id, {
        name: edit.name.trim() || undefined,
        role: edit.role.trim() || undefined,
        system_prompt: selected.is_system ? undefined : edit.system_prompt.trim() || undefined,
        daily_budget_usd: Number.isFinite(budget) && budget >= 0 ? budget : undefined,
        model_provider: edit.model_provider || null,
        model_name: edit.model_provider ? edit.model_name.trim() : null,
      })
      toast.success("Teammate updated", edit.name)
    } catch (err) {
      toast.error("Could not update the bot", errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!selected || selected.is_system) return
    try {
      await bots.deleteBot(selected.id)
      toast.success("Teammate removed", selected.name)
    } catch (err) {
      toast.error("Could not delete the bot", errorMessage(err))
    } finally {
      setConfirmDelete(false)
    }
  }

  const editBudget = Number(edit.daily_budget_usd)

  return (
    <section className="panel" id="panel-builder" role="tabpanel" aria-labelledby="nav-tab-builder">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Workspace</div>
          <h2 className="panel__title">Builder</h2>
          <p className="panel__subtitle">
            Hire a teammate beyond the five specialists. It gets its own Linux desktop, its own daily cap, and the same
            rule as everyone else: it stops before anything consequential.
          </p>
        </div>
        <div className="panel__header-actions">
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => void reseed()}
            disabled={reseeding}
            title="Re-apply bots/*.yaml on the backend without restarting it — creates any new system bot, reconciles existing ones"
          >
            {reseeding ? <Spinner inline label="Reseeding" /> : "Reseed system bots"}
          </button>
        </div>
      </header>

      <div className="panel__body">
        {bots.error && bots.bots.length === 0 ? (
          <ErrorState error={bots.error} title="Bots unavailable" onRetry={() => void bots.refetch()} />
        ) : null}

        <section className="subpanel">
          <h3 className="subpanel__title">New teammate</h3>

          <div className="builder__templates" role="group" aria-label="Start from a template">
            {BOT_TEMPLATES.map((template) => (
              <button
                key={template.id}
                type="button"
                className={cx("chip", appliedTemplate === template.id && "chip--active")}
                onClick={() => applyTemplate(template)}
              >
                {template.label}
              </button>
            ))}
          </div>

          {/*
            Form on the left, the thing being made on the right. The preview is
            not decoration — it is where the desktop profile, the cap and the
            approval policy become legible, and none of the three were on this
            screen before.
          */}
          <div className="builder__split">
            <div className="card">
              <div className="form-grid">
                <label className="field">
                  <span className="field__label">Name</span>
                  <input
                    className="input"
                    value={draft.name}
                    onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                    placeholder="Invoice Chaser"
                  />
                </label>
                <label className="field">
                  <span className="field__label">Role</span>
                  <input
                    className="input"
                    value={draft.role}
                    onChange={(event) => setDraft({ ...draft, role: event.target.value })}
                    placeholder="Chases overdue invoices"
                  />
                </label>
                <label className="field">
                  <span className="field__label">Desktop profile</span>
                  <select
                    className="select"
                    value={draft.desktop_profile}
                    onChange={(event) => setDraft({ ...draft, desktop_profile: event.target.value as DesktopProfile })}
                  >
                    {PROFILES.map((profile) => (
                      <option key={profile} value={profile}>
                        {profile}
                      </option>
                    ))}
                  </select>
                  <span className="field__hint">{PROFILE_NOTES[draft.desktop_profile]}</span>
                </label>
                <label className="field">
                  <span className="field__label">Daily budget (USD)</span>
                  <input
                    className={cx("input", budgetInvalid && "input--invalid")}
                    inputMode="decimal"
                    aria-invalid={budgetInvalid}
                    value={draft.daily_budget_usd}
                    onChange={(event) => setDraft({ ...draft, daily_budget_usd: event.target.value })}
                  />
                  <span className="field__hint">It stops working when it reaches this. Leave blank for no cap.</span>
                </label>
              </div>
              <label className="field">
                <span className="field__label">System prompt</span>
                <textarea
                  className="input"
                  rows={6}
                  value={draft.system_prompt}
                  onChange={(event) => setDraft({ ...draft, system_prompt: event.target.value })}
                  placeholder="You are…"
                />
                <span className="field__hint">
                  How it should behave, in its own words. This is the whole of its judgement — everything else on this
                  screen is a boundary around it.
                </span>
              </label>
              <div className="row-actions">
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  onClick={() => void create()}
                  disabled={creating}
                >
                  {creating ? <Spinner inline label="Creating" /> : "Create teammate"}
                </button>
                {missing.length > 0 ? (
                  <span className="builder__requirement">Still needs {missing.join(" and ")}.</span>
                ) : null}
              </div>
            </div>

            <TeammatePreview
              eyebrow="Who you are hiring"
              name={draft.name}
              role={draft.role}
              slug="custom"
              profile={draft.desktop_profile}
              budget={Number.isFinite(draftBudget) && draftBudget > 0 ? draftBudget : null}
            />
          </div>
        </section>

        <section className="subpanel">
          <h3 className="subpanel__title">Edit a teammate</h3>
          <label className="field">
            <span className="field__label">Bot</span>
            <select className="select" value={activeBotId ?? ""} onChange={(event) => onSelectBot(event.target.value)}>
              <option value="">Select a bot…</option>
              {bots.bots.map((bot) => (
                <option key={bot.id} value={bot.id}>
                  {bot.name}
                  {bot.is_system ? " (system)" : ""}
                </option>
              ))}
            </select>
          </label>

          {!selected ? (
            <EmptyState compact glyph="blocks" title="Nothing selected" description="Pick a bot to edit it." />
          ) : (
            <div className="builder__split">
              <div className="card">
                {selected.is_system ? (
                  <div className="notice" role="note">
                    System bot — name, role and budget are editable; the prompt and slug are locked.
                  </div>
                ) : null}
                <div className="form-grid">
                  <label className="field">
                    <span className="field__label">Name</span>
                    <input
                      className="input"
                      value={edit.name}
                      onChange={(event) => setEdit({ ...edit, name: event.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">Role</span>
                    <input
                      className="input"
                      value={edit.role}
                      onChange={(event) => setEdit({ ...edit, role: event.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">Daily budget (USD)</span>
                    <input
                      className="input"
                      inputMode="decimal"
                      value={edit.daily_budget_usd}
                      onChange={(event) => setEdit({ ...edit, daily_budget_usd: event.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">Model</span>
                    <select
                      className="select"
                      value={edit.model_provider}
                      onChange={(event) =>
                        setEdit({
                          ...edit,
                          model_provider: event.target.value as ModelProvider | "",
                          model_name: event.target.value ? edit.model_name : "",
                        })
                      }
                    >
                      <option value="">Default (tier routing)</option>
                      {availableProviders.map((key) => (
                        <option key={key} value={key}>
                          {PROVIDER_LABEL[key]}
                        </option>
                      ))}
                    </select>
                    {edit.model_provider ? (
                      <input
                        className={cx("input", modelOverrideInvalid && "input--invalid")}
                        aria-invalid={modelOverrideInvalid}
                        value={edit.model_name}
                        onChange={(event) => setEdit({ ...edit, model_name: event.target.value })}
                        placeholder="model name, e.g. claude-opus-4-5"
                      />
                    ) : (
                      <span className="field__hint">
                        Follows the router's ordinary tier routing. Pin a provider to send every one of this bot's
                        calls to a specific model instead — see Setup for which providers this backend can reach.
                      </span>
                    )}
                  </label>
                </div>
                <label className="field">
                  <span className="field__label">System prompt</span>
                  <textarea
                    className="input"
                    rows={6}
                    value={edit.system_prompt}
                    disabled={selected.is_system}
                    placeholder={selected.is_system ? "Locked on system bots" : "You are…"}
                    onChange={(event) => setEdit({ ...edit, system_prompt: event.target.value })}
                  />
                  {/*
                    `BotOut` does not return `system_prompt` — see the comment on
                    the field in `packages/protocol`. So this box is blank for a
                    custom bot that certainly has one, and a blank box reads as
                    "this bot has no prompt". Saying so is cheaper and more
                    honest than pretending, and `save()` already treats blank as
                    "leave it alone".
                  */}
                  {!selected.is_system ? (
                    <span className="field__hint">
                      The stored prompt is never sent back by the API, so this starts empty. Leave it empty to keep the
                      current one; anything you type replaces it.
                    </span>
                  ) : null}
                </label>
                <div className="row-actions">
                  <button type="button" className="btn btn--primary btn--sm" onClick={() => void save()} disabled={saving}>
                    {saving ? <Spinner inline label="Saving" /> : "Save changes"}
                  </button>

                  {/*
                    Deleting a teammate stops its container and destroys its home
                    directory. Two presses, with the consequence spelled out
                    between them — the same shape as every other irreversible
                    action in the product, and no native `confirm()` dialog,
                    which is a browser artefact in a packaged desktop app.
                  */}
                  {selected.is_system ? (
                    <span className="builder__requirement">System teammates cannot be removed.</span>
                  ) : confirmDelete ? (
                    <span className="danger-confirm" role="alert">
                      <span className="danger-confirm__text">
                        Stops {selected.name}&apos;s desktop and erases its home directory. There is no undo.
                      </span>
                      <button type="button" className="btn btn--quiet-danger btn--sm" onClick={() => void remove()}>
                        Delete {selected.name}
                      </button>
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        autoFocus
                        onClick={() => setConfirmDelete(false)}
                      >
                        Keep
                      </button>
                    </span>
                  ) : (
                    <button type="button" className="btn btn--ghost btn--sm" onClick={() => setConfirmDelete(true)}>
                      Delete teammate
                    </button>
                  )}
                </div>
              </div>

              <TeammatePreview
                eyebrow="After you save"
                name={edit.name}
                role={edit.role}
                slug={selected.slug}
                profile={selected.desktop_profile}
                budget={Number.isFinite(editBudget) && editBudget > 0 ? editBudget : null}
                isSystem={selected.is_system}
              />
            </div>
          )}

          {selected ? <MemoriesSection bot={selected} /> : null}
        </section>
      </div>
    </section>
  )
}
