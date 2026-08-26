/**
 * First-run (and revisitable) setup: point the app at a backend, see which
 * model providers it can actually reach, and assign a provider/model to a
 * bot where the default tier routing is not what you want.
 *
 * Three steps, and step 2 is deliberately read-only. `MODEL_PROVIDER` /
 * `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / … live in the *backend's*
 * environment (`apps/api/.env` or its container's secrets) — there is no
 * endpoint that lets a client rewrite another process's environment
 * variables at runtime, and there should not be one; that is a server
 * operator's decision, not a click in this app. What this wizard *can* do,
 * and does, is tell you what the backend already has configured
 * (`GET /bots/providers`) and let you assign a bot to one of the providers
 * that are actually live (`PATCH /bots/{id}`).
 */
import { useCallback, useEffect, useState } from "react"
import { brand } from "@nesqbot/ui"
import { API_BASE, DEFAULT_API_BASE, errorMessage, isApiError, setApiBase } from "../api/client"
import { getHealth, getProviders, listBots, updateBot } from "../api/endpoints"
import { useMutation } from "../hooks/useAsync"
import { markSetupComplete } from "../state/setup"
import { cx } from "../lib/format"
import { Icon } from "./Icon"
import { NesqualLockup } from "./Brand"
import { Spinner } from "./Spinner"
import type { Bot, ModelProvider, ProvidersOut } from "../types"

const PROVIDER_LABEL: Record<ModelProvider, string> = {
  azure: "Azure OpenAI",
  openai: "OpenAI (or a local model server)",
  anthropic: "Anthropic",
  google: "Google",
}

const PROVIDER_HINT: Record<ModelProvider, string> = {
  azure: "Azure AI Foundry / Azure OpenAI.",
  openai: "Real OpenAI, or any self-hosted OpenAI-compatible server — Ollama, vLLM, LM Studio, OpenRouter. Same client either way; only the address differs.",
  anthropic: "Claude, via the Messages API.",
  google: "Gemini.",
}

type Step = "endpoint" | "providers" | "bots"

export function SetupWizard({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<Step>("endpoint")
  const [endpointConfirmed, setEndpointConfirmed] = useState(false)

  const finish = useCallback(() => {
    markSetupComplete()
    onDone()
  }, [onDone])

  return (
    <div className="signin-screen">
      <main className="setup-wizard">
        <NesqualLockup size={30} wordmark="continuation" tagline title={brand.companyName} />

        <ol className="setup-wizard__steps" aria-label="Setup progress">
          <li className={cx(step === "endpoint" && "is-active", endpointConfirmed && "is-done")}>1. Backend</li>
          <li className={cx(step === "providers" && "is-active")} aria-disabled={!endpointConfirmed}>
            2. Providers
          </li>
          <li className={cx(step === "bots" && "is-active")} aria-disabled={!endpointConfirmed}>
            3. Bots
          </li>
        </ol>

        {step === "endpoint" ? (
          <EndpointStep
            onConfirmed={() => {
              setEndpointConfirmed(true)
              setStep("providers")
            }}
            onSkip={finish}
          />
        ) : null}

        {step === "providers" ? (
          <ProvidersStep onBack={() => setStep("endpoint")} onNext={() => setStep("bots")} />
        ) : null}

        {step === "bots" ? <BotsStep onBack={() => setStep("providers")} onFinish={finish} /> : null}
      </main>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Step 1 — backend endpoint
 * ------------------------------------------------------------------ */

function EndpointStep({ onConfirmed, onSkip }: { onConfirmed: () => void; onSkip: () => void }) {
  const [url, setUrl] = useState(API_BASE)
  const [tested, setTested] = useState<{ ok: boolean; label: string } | null>(null)
  const test = useMutation(async (candidate: string) => {
    // Not persisted until it actually answers — a typo must not strand every
    // other panel behind a dead endpoint the moment the input loses focus.
    setApiBase(candidate, { persist: false })
    try {
      const health = await getHealth()
      const label = health.build && health.build !== "unknown" ? health.build : (health.version ?? "connected")
      setTested({ ok: true, label })
      return health
    } catch (err) {
      setApiBase(API_BASE, { persist: false }) // revert the probe; nothing here is committed yet
      setTested({ ok: false, label: errorMessage(err) })
      throw err
    }
  })

  const onTest = useCallback(() => {
    setTested(null)
    void test.run(url).catch(() => undefined)
  }, [test, url])

  const onContinue = useCallback(() => {
    setApiBase(url, { persist: true })
    onConfirmed()
  }, [url, onConfirmed])

  return (
    <div className="setup-wizard__step">
      <h1 className="setup-wizard__headline">Where is your Nesq Bot backend?</h1>
      <p className="setup-wizard__body">
        Deploy <code>apps/api</code> anywhere — your own server, a container host, Azure, a laptop on your LAN — and
        point this app at it. Leave the default if the API is running on this machine.
      </p>

      <label className="field">
        <span className="field__label">API address</span>
        <input
          className="input"
          value={url}
          onChange={(event) => {
            setUrl(event.target.value)
            setTested(null)
          }}
          placeholder={DEFAULT_API_BASE}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
        />
      </label>

      <div className="setup-wizard__actions-row">
        <button type="button" className="btn btn--ghost" onClick={onTest} disabled={test.pending || !url.trim()}>
          {test.pending ? <Spinner inline label="Testing…" /> : "Test connection"}
        </button>
        {tested ? (
          <span className={cx("setup-wizard__test-result", tested.ok ? "is-ok" : "is-error")}>
            <Icon name={tested.ok ? "check" : "alert"} size={14} />
            {tested.ok ? `Connected — API ${tested.label}` : tested.label}
          </span>
        ) : null}
      </div>

      <div className="setup-wizard__actions">
        <button type="button" className="btn btn--ghost" onClick={onSkip}>
          Skip setup
        </button>
        <button type="button" className="btn btn--primary" onClick={onContinue} disabled={!tested?.ok}>
          Continue
        </button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Step 2 — which providers the backend can reach (read-only)
 * ------------------------------------------------------------------ */

function ProvidersStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const [providers, setProviders] = useState<ProvidersOut | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    getProviders(controller.signal)
      .then((result) => {
        setProviders(result)
        setError(null)
      })
      .catch((err) => setError(err))
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  const anyLive = providers ? Object.values(providers).some(Boolean) : false

  return (
    <div className="setup-wizard__step">
      <h1 className="setup-wizard__headline">What can this backend reach?</h1>
      <p className="setup-wizard__body">
        A provider is configured on the backend itself — an environment variable, a key in Key Vault — not here. This
        is what it reports right now, so you know which providers step 3 can actually assign to a bot.
      </p>

      {loading ? (
        <Spinner inline label="Checking…" />
      ) : error ? (
        <div className="setup-wizard__error" role="alert">
          <Icon name="alert" size={15} />
          <span>{errorMessage(error)}</span>
        </div>
      ) : (
        <ul className="setup-wizard__providers">
          {(Object.keys(PROVIDER_LABEL) as ModelProvider[]).map((key) => {
            const live = Boolean(providers?.[key])
            return (
              <li key={key} className={cx("setup-wizard__provider", live && "is-live")}>
                <Icon name={live ? "check" : "close"} size={15} />
                <div>
                  <div className="setup-wizard__provider-name">
                    {PROVIDER_LABEL[key]}
                    {!live ? <span className="setup-wizard__provider-badge">not configured</span> : null}
                  </div>
                  <div className="field__hint">{PROVIDER_HINT[key]}</div>
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {!loading && !error && !anyLive ? (
        <p className="setup-wizard__body">
          Nothing is configured yet — every bot will reply with a deterministic mock until at least one provider has
          a credential. See <code>.env.example</code> in the backend for the settings to set (
          <code>AZURE_OPENAI_*</code>, <code>OPENAI_*</code>, …), then come back to this step.
        </p>
      ) : null}

      <div className="setup-wizard__actions">
        <button type="button" className="btn btn--ghost" onClick={onBack}>
          Back
        </button>
        <button type="button" className="btn btn--primary" onClick={onNext}>
          Continue
        </button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Step 3 — per-bot provider/model
 * ------------------------------------------------------------------ */

function BotsStep({ onBack, onFinish }: { onBack: () => void; onFinish: () => void }) {
  const [bots, setBots] = useState<Bot[] | null>(null)
  const [providers, setProviders] = useState<ProvidersOut | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([listBots(controller.signal), getProviders(controller.signal)])
      .then(([botList, providerList]) => {
        setBots(botList)
        setProviders(providerList)
      })
      .catch((err) => setError(err))
    return () => controller.abort()
  }, [])

  const availableProviders = providers
    ? (Object.keys(PROVIDER_LABEL) as ModelProvider[]).filter((key) => providers[key])
    : []

  return (
    <div className="setup-wizard__step">
      <h1 className="setup-wizard__headline">Assign a provider per bot</h1>
      <p className="setup-wizard__body">
        Optional. Left as "Default", a bot follows the router's ordinary tier routing — most deployments never need
        to touch this. Pin one to a specific provider and model when you want, say, the support bot answering on
        Claude while everything else stays on the default.
      </p>

      {error ? (
        <div className="setup-wizard__error" role="alert">
          <Icon name="alert" size={15} />
          <span>{errorMessage(error)}</span>
        </div>
      ) : !bots ? (
        <Spinner inline label="Loading bots…" />
      ) : availableProviders.length === 0 ? (
        <p className="setup-wizard__body">
          No provider is configured yet, so there is nothing to assign — go back and configure one on the backend
          first, or skip this and revisit it later from the Builder tab.
        </p>
      ) : (
        <ul className="setup-wizard__bots">
          {bots.map((bot) => (
            <BotProviderRow key={bot.id} bot={bot} availableProviders={availableProviders} />
          ))}
        </ul>
      )}

      <div className="setup-wizard__actions">
        <button type="button" className="btn btn--ghost" onClick={onBack}>
          Back
        </button>
        <button type="button" className="btn btn--primary" onClick={onFinish}>
          Finish
        </button>
      </div>
    </div>
  )
}

function BotProviderRow({ bot, availableProviders }: { bot: Bot; availableProviders: ModelProvider[] }) {
  const [provider, setProvider] = useState<ModelProvider | "">(bot.model_provider ?? "")
  const [model, setModel] = useState(bot.model_name ?? "")
  const save = useMutation(async () => {
    if (provider) return updateBot(bot.id, { model_provider: provider, model_name: model.trim() })
    return updateBot(bot.id, { model_provider: null, model_name: null })
  })

  const dirty = provider !== (bot.model_provider ?? "") || model !== (bot.model_name ?? "")
  const canSave = dirty && (!provider || model.trim().length > 0)

  return (
    <li className="setup-wizard__bot-row">
      <span className="setup-wizard__bot-name">{bot.name}</span>
      <select
        className="select"
        value={provider}
        onChange={(event) => {
          setProvider(event.target.value as ModelProvider | "")
          save.reset()
        }}
      >
        <option value="">Default (tier routing)</option>
        {availableProviders.map((key) => (
          <option key={key} value={key}>
            {PROVIDER_LABEL[key]}
          </option>
        ))}
      </select>
      {provider ? (
        <input
          className="input"
          value={model}
          onChange={(event) => {
            setModel(event.target.value)
            save.reset()
          }}
          placeholder="model name, e.g. claude-opus-4-5"
        />
      ) : (
        <span />
      )}
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        disabled={!canSave || save.pending}
        onClick={() => void save.run()}
      >
        {save.pending ? <Spinner inline label="Saving…" /> : save.error ? "Retry" : "Save"}
      </button>
      {save.error && !save.pending ? (
        <span className="setup-wizard__row-error">{isApiError(save.error) ? save.error.detail : errorMessage(save.error)}</span>
      ) : null}
    </li>
  )
}
