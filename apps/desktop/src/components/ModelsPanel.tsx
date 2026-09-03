/**
 * Which models this install can actually reach, and who is pinned to what.
 *
 * Two questions that used to be answerable only from inside the bot edit form,
 * one bot at a time: *is this provider live?* and *which of my teammates is
 * pinned off the router?* The second one matters more than it sounds — a bot
 * pinned to a provider whose key was later removed does not fall back to tier
 * routing, it fails, and there was no screen anywhere that would show you the
 * pinning and the availability side by side.
 *
 * The credentials editor is the same component the Builder uses, not a second
 * copy of it: one save path, one set of provider hints.
 */
import { ProviderCredentialsSection, useAvailableProviders } from "./BuilderPanel"
import { EmptyState } from "./EmptyState"
import type { BotsApi } from "../hooks/useBots"
import type { ModelProvider } from "../types"

const PROVIDER_LABEL: Record<ModelProvider, string> = {
  azure: "Azure OpenAI",
  openai: "OpenAI / OpenAI-compatible",
  anthropic: "Anthropic",
  google: "Google",
}

export function ModelsPanel({ bots }: { bots: BotsApi }) {
  const [available, refetchAvailable] = useAvailableProviders()
  const pinned = bots.bots.filter((bot) => bot.model_provider)

  return (
    <div className="settings__page">
      <section className="settings__group">
        <h3 className="settings__group-title">Providers</h3>
        <p className="settings__note">
          Azure AI Foundry, OpenAI, Anthropic, Google, or any OpenAI-compatible endpoint — Ollama,
          vLLM, LM Studio, OpenRouter. A key saved here is encrypted in the database rather than
          written into the backend&apos;s environment, and an operator&apos;s own environment
          variable for the same provider always wins.
        </p>
        <ul className="settings__pills">
          {(Object.keys(PROVIDER_LABEL) as ModelProvider[]).map((provider) => (
            <li key={provider}>
              <span className={available.includes(provider) ? "chip chip--ok" : "chip chip--muted"}>
                {PROVIDER_LABEL[provider]}
                {available.includes(provider) ? " · live" : " · no credential"}
              </span>
            </li>
          ))}
        </ul>
        <ProviderCredentialsSection onSaved={refetchAvailable} />
      </section>

      <section className="settings__group">
        <h3 className="settings__group-title">Pinned teammates</h3>
        <p className="settings__note">
          Everybody else follows the router: cheap work on the small model, reasoning on the large
          one, and a fallback tier when the large one is throttled. Pinning a bot sends every one of
          its calls to one model — which is also why a pin whose provider has no credential fails
          rather than quietly falling back.
        </p>
        {pinned.length === 0 ? (
          <EmptyState
            compact
            glyph="spark"
            title="Nobody is pinned"
            description="Every teammate is on tier routing. Pin one from its profile if you need to."
          />
        ) : (
          <ul className="settings__rows">
            {pinned.map((bot) => {
              const live = available.includes(bot.model_provider as ModelProvider)
              return (
                <li key={bot.id} className="settings__row">
                  <span className="settings__row-name">{bot.name}</span>
                  <code className="settings__row-value">
                    {bot.model_provider} · {bot.model_name}
                  </code>
                  <span className={live ? "chip chip--ok" : "chip chip--warn"}>
                    {live ? "reachable" : "no credential for this provider"}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </div>
  )
}
