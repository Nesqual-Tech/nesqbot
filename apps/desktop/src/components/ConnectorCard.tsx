import { useEffect, useState } from "react"
import { riskDescriptions, riskLabels, type RiskClass } from "@nesqbot/ui"
import { cx, plural } from "../lib/format"
import { gatedCount, highestRisk, tallyRisks } from "../lib/risk"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"
import type { Connector, ConnectorBinding } from "../types"

export interface ConnectorCardProps {
  connector: Connector
  binding?: ConnectorBinding
  botName: string | null
  busy: boolean
  onBind: (secretRef: string) => Promise<void>
  onUnbind: () => Promise<void>
  onDelete?: () => Promise<void>
}

/** See `ApprovalCard`: the six risk classes wear the design system's roles. */
function RiskChip({ risk }: { risk: string }) {
  const known = risk as RiskClass
  return (
    <span className="chip chip--risk" data-risk={risk} title={riskDescriptions[known] ?? undefined}>
      {riskLabels[known] ?? risk}
    </span>
  )
}

/**
 * One connector in the catalogue.
 *
 * ## What this card is for
 *
 * Binding a connector to a bot is a *grant*. It is the moment a teammate goes
 * from being unable to touch Stripe to being able to issue refunds through it,
 * and it is therefore one of the two or three most consequential things anyone
 * does in this product. The card used to describe that decision as
 * `c-stripe · auth api_key · 2 actions` in muted grey, with the risk classes
 * folded away behind a "Show actions" button and a permanently-unfolded secret
 * field and a red "Delete connector" sitting under every entry in the
 * catalogue whether or not anyone had asked to delete anything.
 *
 * So it now says the same thing the approval card says, in the same places:
 *
 *  - the worst class the connector carries is the card's identity — the edge
 *    band and the eyebrow word, so `stripe` (spend) and `hubspot` (read only)
 *    are different objects at a glance;
 *  - a tally of what it can do, on the face, rather than one click away;
 *  - the governance promise stated out loud — "2 of these always stop for your
 *    approval" — because that sentence is the entire reason this is safe;
 *  - and the form is folded until you ask for it, with the destructive action
 *    inside the fold and behind an inline confirmation rather than shouting
 *    from the resting state.
 */
export function ConnectorCard({ connector, binding, botName, busy, onBind, onUnbind, onDelete }: ConnectorCardProps) {
  const [secretRef, setSecretRef] = useState(String(binding?.secret_ref ?? ""))
  const [showActions, setShowActions] = useState(false)
  const [editing, setEditing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const bound = Boolean(binding)
  const status = String(binding?.status ?? (bound ? "connected" : "not bound"))
  const statusTone = status === "connected" ? "ok" : status === "error" ? "error" : "idle"

  const actions = connector.actions ?? []
  const worst = highestRisk(actions, connector.risk_default)
  const tally = tallyRisks(actions)
  const gated = gatedCount(actions)

  // A binding arriving from elsewhere (or a bot switch) resets the form.
  useEffect(() => {
    setSecretRef(String(binding?.secret_ref ?? ""))
    setEditing(false)
    setConfirmDelete(false)
  }, [binding?.secret_ref, binding?.bot_id])

  const actionsId = `connector-actions-${connector.id}`

  return (
    <article className="card connector" data-risk={worst} aria-label={`Connector ${connector.name}`}>
      <header className="connector__header">
        <div className="connector__lead">
          <div className="connector__risk-word" title={riskDescriptions[worst] ?? undefined}>
            {riskLabels[worst] ?? worst}
          </div>
          <h3 className="connector__name">
            {connector.name}
            {connector.first_party ? <span className="chip chip--muted">first-party</span> : null}
          </h3>
        </div>
        <span className={cx("chip", `chip--${statusTone}`)}>{bound && botName ? `bound · ${botName}` : status}</span>
      </header>

      <p className="connector__facts">
        <code>{connector.id}</code>
        <span aria-hidden="true">·</span>
        {/*
          A manifest with no `auth` is a real shape — the catalogue serves rows
          that predate the field — and `auth ${undefined}` printed the word
          "undefined" on the card. Say nothing rather than say that.
        */}
        {connector.auth ? (connector.auth === "none" ? "no auth" : `auth ${connector.auth}`) : "auth not declared"}
        {connector.version ? (
          <>
            <span aria-hidden="true">·</span>v{connector.version}
          </>
        ) : null}
      </p>

      {/*
        What it can do, ranked. Most-dangerous-first, the same ordering the
        approval queue's tally uses, so the eye lands on `spend` before it
        lands on `observe` on every surface in the app.
      */}
      {tally.length > 0 ? (
        <ul className="connector__tally" aria-label="What this connector can do">
          {tally.map((entry) => (
            <li className="connector__tally-item" data-risk={entry.risk} key={entry.risk}>
              <span className="connector__tally-pip" aria-hidden="true" />
              <span className="connector__tally-n">{entry.count}</span>
              <span className="connector__tally-label">{(riskLabels[entry.risk] ?? entry.risk).toLowerCase()}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No actions declared — a bot bound to this cannot do anything with it.</p>
      )}

      {/*
        The promise, said out loud on the surface where the capability is
        granted rather than only on the surface where it is exercised.
      */}
      {gated > 0 ? (
        <p className="connector__gate">
          <Icon name="shield" size={13} />
          {gated === actions.length
            ? `Every one of these stops for your approval.`
            : `${gated} of these ${gated === 1 ? "stops" : "stop"} for your approval.`}
        </p>
      ) : actions.length > 0 ? (
        <p className="connector__gate connector__gate--free">
          <Icon name="check" size={13} />
          Nothing here needs approval — it can only read and draft.
        </p>
      ) : null}

      {actions.length > 0 ? (
        <>
          <button
            type="button"
            className="disclosure"
            aria-expanded={showActions}
            aria-controls={actionsId}
            onClick={() => setShowActions((prev) => !prev)}
          >
            <Icon name={showActions ? "collapse" : "expand"} size={14} />
            {showActions ? "Hide the actions" : `Name the ${plural(actions.length, "action")}`}
          </button>
          {showActions ? (
            <ul className="connector__actions reveal" id={actionsId}>
              {actions.map((action) => (
                <li key={action.name}>
                  <code>{action.name}</code>
                  <RiskChip risk={action.risk} />
                  {action.description ? <span className="muted">{action.description}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}

      <div className="connector__bind">
        {!editing ? (
          <div className="connector__resting">
            {bound ? (
              <p className="connector__bound-line">
                <Icon name="plug" size={13} />
                <span>
                  Bound to {botName ?? "this bot"}
                  {binding?.secret_ref ? (
                    <>
                      {" · "}
                      <code>{binding.secret_ref}</code>
                    </>
                  ) : (
                    " · no secret ref"
                  )}
                </span>
              </p>
            ) : null}
            <div className="row-actions">
              {bound ? (
                <>
                  <button type="button" className="btn btn--ghost btn--sm" onClick={() => setEditing(true)}>
                    Change binding
                  </button>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    disabled={busy || !botName}
                    onClick={() => void onUnbind()}
                  >
                    {busy ? <Spinner inline label="Working" /> : "Unbind"}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  disabled={!botName}
                  onClick={() => setEditing(true)}
                  title={botName ? `Bind to ${botName}` : "Select a bot first"}
                >
                  {botName ? `Bind to ${botName}` : "Select a bot to bind"}
                </button>
              )}
            </div>
          </div>
        ) : (
          <div className="connector__form reveal">
            <label className="field field--inline">
              <span className="field__label">Secret ref</span>
              <input
                className="input"
                value={secretRef}
                placeholder="kv://nesqbot/connector-secret"
                autoFocus
                onChange={(event) => setSecretRef(event.target.value)}
                disabled={!botName}
              />
            </label>
            <p className="connector__hint">
              The credential stays in the vault. Nesq Bot stores the reference, never the secret.
            </p>
            <div className="row-actions">
              <button
                type="button"
                className="btn btn--primary btn--sm"
                disabled={busy || !botName}
                onClick={async () => {
                  await onBind(secretRef)
                  setEditing(false)
                }}
              >
                {busy ? <Spinner inline label="Working" /> : bound ? "Update binding" : `Bind to ${botName ?? "bot"}`}
              </button>
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setEditing(false)}>
                Cancel
              </button>

              {/*
                Deleting a connector removes it from the catalogue for every
                bot, so it lives inside the fold and takes two presses. Same
                rule the Bot Desktop's "Stop + wipe" follows: danger belongs in
                the word and the confirmation, not in a permanent block of red
                on a card nobody came here to delete.
              */}
              {onDelete && !connector.first_party ? (
                confirmDelete ? (
                  <span className="danger-confirm" role="alert">
                    <span className="danger-confirm__text">Removes {connector.name} for every bot. No undo.</span>
                    <button
                      type="button"
                      className="btn btn--quiet-danger btn--sm"
                      disabled={busy}
                      onClick={() => void onDelete()}
                    >
                      Delete it
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
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm connector__delete"
                    disabled={busy}
                    onClick={() => setConfirmDelete(true)}
                  >
                    Delete connector
                  </button>
                )
              ) : null}
            </div>
          </div>
        )}
      </div>
    </article>
  )
}
