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
  /**
   * Hand the server the credential *itself*, rather than a reference to one
   * somebody already created in Key Vault by hand. The server decides where it
   * lands — vault when its identity may write there, encrypted in its own
   * database when it may not — and says which, which is why this card must not
   * promise a destination before the answer comes back.
   */
  onStoreValue: (value: string) => Promise<void>
  onUnbind: () => Promise<void>
  onDelete?: () => Promise<void>
}

/**
 * Where the credential behind a reference actually lives, from the reference.
 *
 * The API answers this too (`BotConnectorOut.secret_backend`), and it is the
 * same derivation — `secrets.describe_backend`. It is repeated here rather
 * than read off the binding because `ConnectorBinding` is declared in
 * `packages/protocol`, which this change does not own; the ref itself is in
 * the type and carries the whole answer.
 */
function backendOf(ref: string | null | undefined): "key_vault" | "app_encrypted" | "env" | "none" {
  const value = (ref ?? "").trim().toLowerCase()
  if (!value) return "none"
  if (value.startsWith("kv://")) return "key_vault"
  if (value.startsWith("app://")) return "app_encrypted"
  if (value.startsWith("env://")) return "env"
  // A bare name resolves against the server's configured vault.
  return "key_vault"
}

/** One sentence, per backend, that is true. Never "the credential stays in the
 *  vault" when it did not. */
const BACKEND_LINE: Record<ReturnType<typeof backendOf>, string> = {
  key_vault: "Held in Key Vault — this app stores only the reference.",
  app_encrypted: "Encrypted in Nesq Bot's own database — Key Vault would not take the write.",
  env: "Read from the server's environment at run time.",
  none: "No credential — this connector runs unauthenticated.",
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
export function ConnectorCard({
  connector,
  binding,
  botName,
  busy,
  onBind,
  onStoreValue,
  onUnbind,
  onDelete,
}: ConnectorCardProps) {
  const [secretRef, setSecretRef] = useState(String(binding?.secret_ref ?? ""))
  const [secretValue, setSecretValue] = useState("")
  /*
   * Two ways to bind, and the reference is not the default.
   *
   * "Type the value" is what people arrive wanting to do; "paste a reference"
   * is what the app used to be able to do at all, and it stays because anyone
   * already running with hand-made Key Vault secrets must not be broken by
   * this. A connector that already has a reference opens on that mode, so
   * "Change binding" still lands where its ref is.
   */
  const [mode, setMode] = useState<"value" | "ref">("value")
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

  const backend = backendOf(binding?.secret_ref)

  // A binding arriving from elsewhere (or a bot switch) resets the form. The
  // typed value is cleared here too: leaving a credential sitting in component
  // state after it has been stored is a copy of it nobody asked for.
  useEffect(() => {
    setSecretRef(String(binding?.secret_ref ?? ""))
    setSecretValue("")
    // A binding whose value the app itself stored opens on "type the value",
    // because the only thing anyone does to one of those is replace it — its
    // `app://` reference is a marker, not something to edit.
    setMode(binding?.secret_ref && backendOf(binding.secret_ref) !== "app_encrypted" ? "ref" : "value")
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
              <>
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
                {/*
                  Where the credential actually is, at rest, on the surface
                  that granted it. The card used to print the reference and
                  stop, which was fine while a reference could only ever mean
                  Key Vault. It cannot any more: the server falls back to
                  encrypting the value in its own database when the vault
                  refuses the write, and a card that stays silent about that
                  lets someone go on believing their key is in a vault.
                */}
                <p className="connector__hint" data-backend={backend}>
                  {BACKEND_LINE[backend]}
                </p>
              </>
            ) : null}
            <div className="row-actions">
              {bound ? (
                <>
                  <button type="button" className="btn btn--ghost btn--sm" onClick={() => setEditing(true)}>
                    {backend === "none" ? "Change binding" : "Replace the credential"}
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
            {/*
              The choice, before the field. Typing the value is the thing the
              app could not do at all until now — a credential had to be
              created in Key Vault by hand first — so it leads; pasting a
              reference is unchanged and still here for everyone already
              working that way.
            */}
            <div className="row-actions" role="group" aria-label="How to supply the credential">
              <button
                type="button"
                className={cx("btn", "btn--sm", mode === "value" ? "btn--primary" : "btn--ghost")}
                aria-pressed={mode === "value"}
                onClick={() => setMode("value")}
              >
                Type the value
              </button>
              <button
                type="button"
                className={cx("btn", "btn--sm", mode === "ref" ? "btn--primary" : "btn--ghost")}
                aria-pressed={mode === "ref"}
                onClick={() => setMode("ref")}
              >
                Paste a reference
              </button>
            </div>

            {mode === "value" ? (
              <>
                <label className="field field--inline">
                  <span className="field__label">Credential</span>
                  <input
                    className="input"
                    type="password"
                    value={secretValue}
                    placeholder="The API key, token or password itself"
                    autoFocus
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => setSecretValue(event.target.value)}
                    disabled={!botName}
                  />
                </label>
                {/*
                  Deliberately a forecast and not a promise. The server tries
                  Key Vault and falls back to encrypting the value itself, and
                  only it knows which happened — the toast and the resting line
                  above report the answer.
                */}
                <p className="connector__hint">
                  Nesq Bot stores this on the server — Key Vault when it is allowed to write there, encrypted in
                  its own database when it is not — and tells you which. This app keeps only the reference.
                </p>
              </>
            ) : (
              <>
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
                  A reference to a credential that already exists — <code>kv://vault/name</code> or{" "}
                  <code>env://NAME</code>. The value itself is refused here; switch to “Type the value” for that.
                </p>
              </>
            )}
            <div className="row-actions">
              <button
                type="button"
                className="btn btn--primary btn--sm"
                disabled={busy || !botName || (mode === "value" && !secretValue.trim())}
                onClick={async () => {
                  if (mode === "value") {
                    await onStoreValue(secretValue)
                    // Not kept after the round trip: the credential is stored
                    // server-side now and a copy in component state is one more
                    // place it can be read out of.
                    setSecretValue("")
                  } else {
                    await onBind(secretRef)
                  }
                  setEditing(false)
                }}
              >
                {busy ? (
                  <Spinner inline label="Working" />
                ) : mode === "value" ? (
                  bound && backend !== "none" ? (
                    "Replace the credential"
                  ) : (
                    "Store the credential"
                  )
                ) : bound ? (
                  "Update binding"
                ) : (
                  `Bind to ${botName ?? "bot"}`
                )}
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
