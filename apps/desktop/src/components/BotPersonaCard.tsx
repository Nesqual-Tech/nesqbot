/**
 * Who a teammate actually is.
 *
 * Reported bluntly: *"The bots have personas, with emails and so on but on the
 * desktop app, i can't see that."* That was literally true twice over. The
 * prompt was write-only across the whole API — `CreateCustomBotIn` and
 * `UpdateBotIn` accept one and nothing ever returned one — so the only things
 * this app could render about a bot were its name and its one-line role; and
 * the persona itself did not exist as data at all. `GET /bots/{id}/persona`
 * closed the first half, `bots.email`/`voice`/`signature`/`desktop_habits` the
 * second, and this is where both surface.
 *
 * Loaded on open rather than with the bot list: a sidebar drawn on every
 * launch has no use for five system prompts.
 *
 * Secrets are shown as references and never resolved. `secret_ref` is the
 * pointer (`kv://vault/name`, `app://connector/…`); the scheme says which store
 * actually holds the value, because a credential the vault refused and that
 * fell back to encrypted-at-rest must not look like one that reached the vault.
 */
import { useEffect, useState } from "react"
import { getBotPersona } from "../api/endpoints"
import { relativeTime } from "../lib/format"
import { BotAvatar } from "./BotAvatar"
import { ErrorState } from "./EmptyState"
import { Spinner } from "./Spinner"
import type { Bot, BotPersona } from "../types"

/**
 * Where a credential lives, read off the reference's own scheme.
 *
 * Derived rather than asked for: the scheme *is* the store
 * (`services.secrets.parse_ref`), so there is nothing to add to the API and
 * nothing that can disagree with it. The distinction matters because a
 * credential the vault refused, which fell back to encrypted-at-rest in
 * Postgres, must not read the same as one that reached the vault.
 */
function credentialHome(ref: string | null): string {
  if (!ref) return "no credential"
  const lower = ref.toLowerCase()
  if (lower.startsWith("kv://")) return "in Key Vault"
  if (lower.startsWith("env://")) return "from the environment"
  if (lower.startsWith("app://")) return "encrypted here"
  return "in Key Vault"
}

export interface BotPersonaCardProps {
  bot: Bot
  onClose: () => void
  /** Shown when the shell can act on them — omitted and the buttons vanish. */
  onOpenComputer?: () => void
  onEditProfile?: () => void
}

export function BotPersonaCard({ bot, onClose, onOpenComputer, onEditProfile }: BotPersonaCardProps) {
  const [persona, setPersona] = useState<BotPersona | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    getBotPersona(bot.id, controller.signal)
      .then((data) => setPersona(data))
      .catch((err: unknown) => {
        // An aborted fetch is this effect cleaning up after itself, not a
        // failure worth showing — the setup wizard used to render
        // "signal is aborted without reason" at the user for exactly this.
        if (controller.signal.aborted) return
        setError(err)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [bot.id])

  const budget = Number(bot.daily_budget_usd ?? 0)
  const spent = persona?.spent_usd_today ?? 0
  // Prefer the freshly loaded row: somebody can edit a persona in the sheet
  // and reopen this card without the bot list having refetched yet.
  const who = persona ?? bot

  return (
    <section className="persona" aria-label={`${bot.name} persona`}>
      <header className="persona__header">
        <BotAvatar bot={bot} size={56} className="persona__avatar" />
        <div>
          <h3 className="persona__name">{bot.name}</h3>
          <p className="persona__role">
            {bot.role || "No role set"} · <code>{bot.slug}</code>
          </p>
        </div>
        <button type="button" className="btn btn--ghost btn--sm persona__close" onClick={onClose}>
          Close
        </button>
      </header>

      {/*
        Said once, here, because it is the single most common misreading of the
        product: the role line is a standing job, not this week's task. People
        edit it expecting to change what a bot is currently doing.
      */}
      <p className="persona__lede">
        That description is their standing job, not a task. Message them what you need — the
        standing rules stay whatever you set here.
      </p>

      {onOpenComputer || onEditProfile ? (
        <div className="persona__actions">
          {onOpenComputer ? (
            <button type="button" className="btn btn--primary btn--sm" onClick={onOpenComputer}>
              Open computer
            </button>
          ) : null}
          {onEditProfile ? (
            <button type="button" className="btn btn--ghost btn--sm" onClick={onEditProfile}>
              Edit profile
            </button>
          ) : null}
        </div>
      ) : null}

      {loading ? <Spinner label="Loading persona" /> : null}
      {error ? <ErrorState error={error} title="Persona unavailable" compact /> : null}

      {persona ? (
        <div className="persona__body">
          <dl className="persona__facts">
            <div>
              <dt>Model</dt>
              <dd>
                {persona.model_provider
                  ? `${persona.model_provider}${persona.model_name ? ` · ${persona.model_name}` : ""}`
                  : "Router default"}
              </dd>
            </div>
            <div>
              <dt>Today</dt>
              <dd>
                ${spent.toFixed(4)} of ${budget.toFixed(2)}
              </dd>
            </div>
            <div>
              <dt>Desktop</dt>
              <dd>{persona.desktop_profile}</dd>
            </div>
          </dl>

          <h4 className="persona__label">Reachable at</h4>
          {/*
            The bot's own address first, and labelled for what it is. It is the
            From line on a draft; nothing arrives at it. Inbound sources are
            the ones that actually deliver, so they follow, and a bot with an
            address but no source is a real and common state that has to read
            as such rather than as a mailbox.
          */}
          {who.email ? (
            <ul className="persona__list">
              <li>
                <strong>{who.email}</strong>
                <span className="persona__meta">
                  identity · drafts and signatures use it, no inbox behind it
                </span>
              </li>
            </ul>
          ) : null}
          {persona.inboxes.length > 0 ? (
            <ul className="persona__list">
              {persona.inboxes.map((inbox) => (
                <li key={inbox.slug}>
                  <strong>{inbox.address ?? inbox.name ?? inbox.slug}</strong>
                  <span className="persona__meta">
                    {inbox.channel} · {inbox.kind}
                    {inbox.enabled ? "" : " · paused"}
                    {inbox.last_event_at ? ` · last ${relativeTime(inbox.last_event_at)}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="persona__empty">
              No inbound channel routes here yet. Add one under Connectors and mail, webhooks or
              messages addressed to it will start this bot.
            </p>
          )}

          {who.voice || who.signature || who.desktop_habits ? (
            <>
              <h4 className="persona__label">How they work</h4>
              <dl className="persona__prose">
                {who.voice ? (
                  <div>
                    <dt>Voice</dt>
                    <dd>{who.voice}</dd>
                  </div>
                ) : null}
                {who.signature ? (
                  <div>
                    <dt>Signs off</dt>
                    <dd>{who.signature}</dd>
                  </div>
                ) : null}
                {who.desktop_habits ? (
                  <div>
                    <dt>On their machine</dt>
                    <dd>{who.desktop_habits}</dd>
                  </div>
                ) : null}
              </dl>
            </>
          ) : null}

          <h4 className="persona__label">Tools</h4>
          {persona.connectors.length > 0 ? (
            <ul className="persona__list">
              {persona.connectors.map((connector) => (
                <li key={connector.connector_id}>
                  <strong>{connector.name}</strong>
                  <span className="persona__meta">
                    {connector.status} · {credentialHome(connector.secret_ref)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="persona__empty">
              No connectors bound. This bot works through its Agent Computer only.
            </p>
          )}

          <h4 className="persona__label">Standing job</h4>
          <pre className="persona__prompt">{persona.system_prompt}</pre>
        </div>
      ) : null}
    </section>
  )
}
