import { useEffect, useState, type CSSProperties } from "react"
import { getBotColor, riskLabels } from "@nesqbot/ui"
import { errorMessage } from "../api/client"
import { getProviders } from "../api/endpoints"
import type { BotsApi } from "../hooks/useBots"
import { cx, initials, usd } from "../lib/format"
import { GATED_RISKS } from "../lib/risk"
import { useToast } from "../state/AppState"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"
import type { Bot, DesktopProfile, ModelProvider } from "../types"

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
      </header>

      <div className="panel__body">
        {bots.error && bots.bots.length === 0 ? (
          <ErrorState error={bots.error} title="Bots unavailable" onRetry={() => void bots.refetch()} />
        ) : null}

        <section className="subpanel">
          <h3 className="subpanel__title">New teammate</h3>

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
        </section>
      </div>
    </section>
  )
}
