import { useMemo, useRef, useState } from "react"
import { riskLabels } from "@nesqbot/ui"
import { errorMessage } from "../api/client"
import { useConnectors } from "../hooks/useConnectors"
import { dur, ease, gsap, stagger, useGSAP } from "../lib/motion"
import { byRisk, gatedCount, highestRisk, tallyRisks } from "../lib/risk"
import { useToast } from "../state/AppState"
import { ConnectorCard } from "./ConnectorCard"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { McpPanel } from "./McpPanel"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import type { Bot, ConnectorAuthKind, RegisterConnectorInput, RiskClass } from "../types"

export interface IntegrationsPanelProps {
  bots: Bot[]
  activeBotId: string | null
  onSelectBot: (botId: string) => void
}

const RISKS: RiskClass[] = ["observe", "draft", "mutate", "send", "spend", "delete"]
const AUTH_KINDS = ["oauth2", "api_key", "none"]

export type ManifestValidation =
  { ok: true; value: RegisterConnectorInput; warnings: string[] } | { ok: false; errors: string[] }

/** Validate a pasted connector manifest before it is ever sent to the API. */
export function validateManifest(text: string): ManifestValidation {
  const errors: string[] = []
  const warnings: string[] = []

  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (err) {
    return { ok: false, errors: [`Not valid JSON: ${err instanceof Error ? err.message : "parse error"}`] }
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: false, errors: ["The manifest must be a JSON object."] }
  }

  const obj = parsed as Record<string, unknown>
  const id = typeof obj.id === "string" ? obj.id.trim() : ""
  const name = typeof obj.name === "string" ? obj.name.trim() : ""

  if (!id) errors.push("`id` is required and must be a string.")
  else if (!/^[a-z0-9_]+$/.test(id)) errors.push("`id` must be lowercase snake_case (a-z, 0-9, _).")
  if (!name) errors.push("`name` is required and must be a string.")

  const auth = typeof obj.auth === "string" ? obj.auth : "api_key"
  if (!AUTH_KINDS.includes(auth)) errors.push(`\`auth\` must be one of ${AUTH_KINDS.join(", ")}.`)

  const riskDefault = typeof obj.risk_default === "string" ? obj.risk_default : "observe"
  if (!RISKS.includes(riskDefault as RiskClass)) {
    errors.push(`\`risk_default\` must be one of ${RISKS.join(", ")}.`)
  }

  const scopes = Array.isArray(obj.scopes) ? obj.scopes.filter((s): s is string => typeof s === "string") : []
  if (obj.scopes !== undefined && !Array.isArray(obj.scopes)) errors.push("`scopes` must be an array of strings.")

  const rawActions = obj.actions
  const actions: RegisterConnectorInput["actions"] = []
  if (rawActions !== undefined && !Array.isArray(rawActions)) {
    errors.push("`actions` must be an array.")
  } else if (Array.isArray(rawActions)) {
    rawActions.forEach((entry, index) => {
      if (typeof entry !== "object" || entry === null) {
        errors.push(`actions[${index}] must be an object.`)
        return
      }
      const action = entry as Record<string, unknown>
      const actionName = typeof action.name === "string" ? action.name.trim() : ""
      if (!actionName) {
        errors.push(`actions[${index}].name is required.`)
        return
      }
      const risk = typeof action.risk === "string" ? action.risk : riskDefault
      if (!RISKS.includes(risk as RiskClass)) {
        errors.push(`actions[${index}].risk must be one of ${RISKS.join(", ")}.`)
      }
      if (action.input_schema !== undefined && typeof action.input_schema !== "object") {
        errors.push(`actions[${index}].input_schema must be an object.`)
      }
      actions.push({
        name: actionName,
        description: typeof action.description === "string" ? action.description : "",
        risk: risk as RiskClass,
        input_schema: (action.input_schema as Record<string, unknown> | undefined) ?? {},
      })
    })
  }

  if (actions.length === 0) warnings.push("No actions declared — the bot will not be able to do anything with it.")
  if (obj.first_party === true) warnings.push("`first_party` is ignored: custom connectors register as third-party.")

  if (errors.length > 0) return { ok: false, errors }

  return {
    ok: true,
    warnings,
    value: {
      id,
      name,
      version: typeof obj.version === "string" ? obj.version : "1.0.0",
      // validated against AUTH_KINDS above
      auth: auth as ConnectorAuthKind,
      scopes,
      actions,
      risk_default: riskDefault as RiskClass,
      first_party: false,
    },
  }
}

const SAMPLE_MANIFEST = `{
  "id": "acme_crm",
  "name": "Acme CRM",
  "version": "1.0.0",
  "auth": "api_key",
  "scopes": ["contacts.read"],
  "risk_default": "observe",
  "actions": [
    { "name": "list_contacts", "description": "List CRM contacts", "risk": "observe", "input_schema": {} }
  ]
}`

export function IntegrationsPanel({ bots, activeBotId, onSelectBot }: IntegrationsPanelProps) {
  const toast = useToast()
  const connectors = useConnectors(activeBotId)
  const [manifestText, setManifestText] = useState("")
  const [registering, setRegistering] = useState(false)
  const [busyConnector, setBusyConnector] = useState<string | null>(null)
  const [registerOpen, setRegisterOpen] = useState(false)
  const catalogRef = useRef<HTMLElement | null>(null)
  const landed = useRef(false)

  const activeBot = useMemo(() => bots.find((b) => b.id === activeBotId) ?? null, [bots, activeBotId])
  const validation = useMemo(() => (manifestText.trim() ? validateManifest(manifestText) : null), [manifestText])

  /*
   * The catalogue, in the order it should be read.
   *
   * Bound first, because "what can this teammate already reach" is the
   * question somebody arrives at this panel with. Then most-dangerous-first
   * inside each group, which is the same rule the approval queue sorts by: a
   * connector that can commit money should never be below one that can only
   * read, in either half of the list.
   */
  const ordered = useMemo(() => {
    return [...connectors.connectors].sort((a, b) => {
      const aBound = connectors.bindings[a.id] ? 0 : 1
      const bBound = connectors.bindings[b.id] ? 0 : 1
      if (aBound !== bBound) return aBound - bBound
      const rank = byRisk(highestRisk(a.actions ?? [], a.risk_default), highestRisk(b.actions ?? [], b.risk_default))
      return rank || a.name.localeCompare(b.name)
    })
  }, [connectors.connectors, connectors.bindings])

  /* What the catalogue grants, in total. The panel's headline. */
  const reach = useMemo(() => {
    const boundIds = Object.keys(connectors.bindings)
    const boundActions = ordered
      .filter((connector) => connectors.bindings[connector.id])
      .flatMap((connector) => connector.actions ?? [])
    return {
      total: ordered.length,
      bound: boundIds.length,
      tally: tallyRisks(boundActions),
      gated: gatedCount(boundActions),
      actions: boundActions.length,
    }
  }, [ordered, connectors.bindings])

  /*
   * The catalogue lands once.
   *
   * Same rule as the teammate list: a grid that restages itself every time a
   * binding changes is fidget, not polish. `landed` means binding Stripe does
   * not re-animate HubSpot sitting beside it.
   */
  useGSAP(
    () => {
      if (landed.current || ordered.length === 0) return
      landed.current = true
      gsap.from(".connector", {
        y: 8,
        autoAlpha: 0,
        duration: dur("base"),
        ease: ease("entrance"),
        stagger: stagger(0.04),
      })
    },
    { dependencies: [ordered.length], scope: catalogRef },
  )

  const register = async () => {
    if (!validation) return
    if (!validation.ok) {
      toast.error("Manifest is not valid", validation.errors[0])
      return
    }
    setRegistering(true)
    try {
      const created = await connectors.registerConnector(validation.value)
      toast.success("Connector registered", created.name)
      setManifestText("")
      setRegisterOpen(false)
    } catch (err) {
      toast.error("Could not register connector", errorMessage(err))
    } finally {
      setRegistering(false)
    }
  }

  const bind = async (connectorId: string, secretRef: string) => {
    setBusyConnector(connectorId)
    try {
      await connectors.bind(connectorId, { secret_ref: secretRef.trim() || null, status: "connected" })
      toast.success("Connector bound", `${connectorId} → ${activeBot?.name ?? "bot"}`)
    } catch (err) {
      toast.error("Bind failed", errorMessage(err))
    } finally {
      setBusyConnector(null)
    }
  }

  const unbind = async (connectorId: string) => {
    setBusyConnector(connectorId)
    try {
      await connectors.unbind(connectorId)
      toast.info("Connector unbound", connectorId)
    } catch (err) {
      toast.error("Unbind failed", errorMessage(err))
    } finally {
      setBusyConnector(null)
    }
  }

  const removeConnector = async (connectorId: string) => {
    setBusyConnector(connectorId)
    try {
      await connectors.deleteConnector(connectorId)
      toast.success("Connector deleted", connectorId)
    } catch (err) {
      toast.error("Delete failed", errorMessage(err))
    } finally {
      setBusyConnector(null)
    }
  }

  const showSummary = !connectors.initialising && ordered.length > 0

  return (
    <section className="panel" id="panel-integrations" role="tabpanel" aria-labelledby="nav-tab-integrations">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Workspace</div>
          <h2 className="panel__title">Integrations</h2>
          <p className="panel__subtitle">What each teammate is allowed to reach, and what it may do there.</p>
        </div>
        <div className="panel__header-actions">
          <label className="sr-only" htmlFor="integrations-bot">
            Bot to bind
          </label>
          <select
            id="integrations-bot"
            className="select"
            value={activeBotId ?? ""}
            onChange={(event) => onSelectBot(event.target.value)}
          >
            <option value="">Select a bot…</option>
            {bots.map((bot) => (
              <option key={bot.id} value={bot.id}>
                {bot.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => {
              void connectors.refetch()
              void connectors.refetchBindings()
            }}
            disabled={connectors.loading}
          >
            {connectors.loading ? <Spinner inline label="Refreshing" /> : "Refresh"}
          </button>
        </div>
      </header>

      {/*
        The grant, before you read any of it.

        Not "3 connectors" — the number that matters is what the *selected bot*
        can currently do to the outside world, and how much of it stops at a
        human. That is the sentence this product is sold on, and this was the
        one governance surface that never said it.
      */}
      {showSummary ? (
        <div className="queue-summary summary-strip">
          {reach.bound === 0 ? (
            <p className="queue-summary__count">
              <strong>{reach.total}</strong>
              {reach.total === 1 ? "connector available" : "connectors available"}
              {activeBot ? ` · ${activeBot.name} is bound to none of them` : " · no bot selected"}
            </p>
          ) : (
            <p className="queue-summary__count">
              <strong>{reach.bound}</strong>
              {reach.bound === 1 ? "connector bound" : "connectors bound"}
              {activeBot ? ` to ${activeBot.name}` : ""}
              {reach.total > reach.bound ? ` · ${reach.total - reach.bound} more available` : ""}
            </p>
          )}
          <ul className="queue-summary__tally">
            {reach.tally.map((entry) => (
              <li className="queue-summary__item" data-risk={entry.risk} key={entry.risk}>
                <span className="queue-summary__pip" />
                <span className="queue-summary__n">{entry.count}</span>
                <span className="queue-summary__label">{riskLabels[entry.risk] ?? entry.risk}</span>
              </li>
            ))}
            {reach.bound > 0 ? (
              <li className="queue-summary__item" data-tone={reach.gated > 0 ? "ok" : "warning"}>
                <span className="queue-summary__pip" />
                <span className="queue-summary__label">
                  {reach.gated > 0
                    ? `${reach.gated} of ${reach.actions} need your approval`
                    : "nothing here needs approval"}
                </span>
              </li>
            ) : null}
          </ul>
        </div>
      ) : null}

      <div className="panel__body">
        {!activeBot ? (
          <div className="notice" role="note">
            Pick a bot above to bind connectors to it. The catalog below is shared.
          </div>
        ) : null}

        {connectors.bindingsError ? (
          <ErrorState
            error={connectors.bindingsError}
            title="Bindings unavailable"
            onRetry={() => void connectors.refetchBindings()}
            compact
          />
        ) : null}

        {/*
          The catalogue is a grid, not a column. Three connectors used to fill
          eight hundred vertical pixels in a pane over a thousand wide, each one
          with its bind form permanently unfolded beside a screenful of nothing.
        */}
        <section className="subpanel subpanel--grid" ref={catalogRef}>
          <h3 className="subpanel__title">Connector catalog</h3>

          {connectors.initialising ? <SkeletonCards cards={3} /> : null}

          {connectors.error && connectors.connectors.length === 0 && !connectors.initialising ? (
            <ErrorState
              error={connectors.error}
              title="Catalog unavailable"
              onRetry={() => void connectors.refetch()}
            />
          ) : null}

          {!connectors.initialising && !connectors.error && connectors.connectors.length === 0 ? (
            <EmptyState compact glyph="plug" title="No connectors" description="Register one from a manifest below." />
          ) : null}

          {ordered.map((connector) => (
            <ConnectorCard
              key={connector.id}
              connector={connector}
              binding={connectors.bindings[connector.id]}
              botName={activeBot?.name ?? null}
              busy={busyConnector === connector.id}
              onBind={(secretRef) => bind(connector.id, secretRef)}
              onUnbind={() => unbind(connector.id)}
              onDelete={connector.first_party ? undefined : () => removeConnector(connector.id)}
            />
          ))}
        </section>

        {/*
          Registering a custom connector is a once-a-quarter action that owned
          a ten-row code editor at the bottom of a panel people open to check a
          binding. Folded, it costs one line; open, it is exactly what it was.
        */}
        <section className="subpanel">
          <button
            type="button"
            className="disclosure disclosure--section"
            aria-expanded={registerOpen}
            aria-controls="register-connector"
            onClick={() => setRegisterOpen((prev) => !prev)}
          >
            <Icon name={registerOpen ? "collapse" : "plus"} size={14} />
            {registerOpen ? "Close the manifest editor" : "Register a custom connector from a manifest"}
          </button>

          {registerOpen ? (
            <div className="card reveal" id="register-connector">
              <label className="field">
                <span className="field__label">Manifest JSON</span>
                <textarea
                  className="input input--code"
                  rows={10}
                  spellCheck={false}
                  value={manifestText}
                  placeholder={SAMPLE_MANIFEST}
                  onChange={(event) => setManifestText(event.target.value)}
                  aria-invalid={validation ? !validation.ok : undefined}
                  aria-describedby="manifest-feedback"
                />
              </label>

              <div id="manifest-feedback" aria-live="polite">
                {validation && !validation.ok ? (
                  <ul className="validation validation--error">
                    {validation.errors.map((error) => (
                      <li key={error}>{error}</li>
                    ))}
                  </ul>
                ) : null}
                {validation && validation.ok ? (
                  <ul className="validation validation--ok">
                    <li>
                      Valid manifest — {validation.value.actions?.length ?? 0} action(s), auth {validation.value.auth}.
                    </li>
                    {validation.warnings.map((warning) => (
                      <li key={warning} className="validation__warn">
                        {warning}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              <div className="row-actions">
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  onClick={() => void register()}
                  disabled={registering || !validation?.ok}
                >
                  {registering ? <Spinner inline label="Registering" /> : "Register connector"}
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() => setManifestText(SAMPLE_MANIFEST)}
                >
                  Insert example
                </button>
              </div>
            </div>
          ) : null}
        </section>

        <McpPanel botId={activeBotId} botName={activeBot?.name ?? null} />
      </div>
    </section>
  )
}
