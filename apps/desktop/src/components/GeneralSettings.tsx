/**
 * The settings that are about this *install* rather than about a bot.
 *
 * Where the API is, which way the theme goes, what the keyboard does, and one
 * paragraph explaining what the Agent Computer actually is — that last one
 * earns its place because the pane is the single most misread thing in the
 * product. People find a Linux desktop attached to their chat window and
 * assume it is a second messenger, or that all the bots share it.
 *
 * The connection field writes through `setApiBase` and then *checks* it, in
 * that order, because an endpoint that saves and silently fails is how
 * somebody ends up staring at "API unreachable" with no idea which of the two
 * things they changed broke it.
 */
import { useState } from "react"
import { API_BASE, DEFAULT_API_BASE, errorMessage, setApiBase } from "../api/client"
import { getHealth } from "../api/endpoints"
import { openSetup } from "../App"
import { useToast } from "../state/AppState"
import { useTheme } from "../state/theme"
import { cx } from "../lib/format"

const KEYS: Array<[string, string]> = [
  ["Ctrl K", "Command palette"],
  ["Ctrl N", "New conversation"],
  ["Ctrl ,", "Settings"],
  ["Ctrl ⇧ D", "Agent Computer"],
  ["Esc", "Close what is open"],
]

export function GeneralSettings() {
  const toast = useToast()
  const { theme, setTheme } = useTheme()
  const [url, setUrl] = useState(API_BASE)
  const [checking, setChecking] = useState(false)

  const hosted = url.trim() === DEFAULT_API_BASE

  const saveAndCheck = async () => {
    const next = url.trim()
    if (!next) return
    setChecking(true)
    setApiBase(next, { persist: true })
    try {
      const health = await getHealth()
      toast.success("Connected", `API ${health.build || health.version || "reachable"}`)
    } catch (err) {
      // Left saved on purpose. A typo you can see in the field and correct is
      // better than a rollback that leaves the field showing an address the
      // app is no longer using.
      toast.error("Saved, but could not reach it", errorMessage(err))
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="settings__page">
      <section className="settings__group">
        <h3 className="settings__group-title">Connection</h3>
        <label className="field">
          <span className="field__label">
            API URL {hosted ? <em className="field__note">· Nesqual hosted</em> : null}
          </span>
          <input
            className="input"
            value={url}
            spellCheck={false}
            onChange={(event) => setUrl(event.target.value)}
          />
        </label>
        <div className="settings__actions">
          <button
            type="button"
            className="btn btn--primary btn--sm"
            onClick={() => void saveAndCheck()}
            disabled={checking || !url.trim()}
          >
            {checking ? "Checking…" : "Save and check"}
          </button>
          <button type="button" className="btn btn--ghost btn--sm" onClick={openSetup}>
            Setup wizard
          </button>
          {!hosted ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => setUrl(DEFAULT_API_BASE)}
            >
              Use the hosted API
            </button>
          ) : null}
        </div>
      </section>

      <section className="settings__group">
        <h3 className="settings__group-title">Appearance</h3>
        <div className="settings__choice" role="radiogroup" aria-label="Theme">
          {(["dark", "light"] as const).map((option) => (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={theme === option}
              className={cx("chip", theme === option && "chip--on")}
              onClick={() => setTheme(option)}
            >
              {option === "dark" ? "Dark" : "Light"}
            </button>
          ))}
        </div>
        <p className="settings__note">
          The same navy-and-mark palette in both schemes. Nothing about a bot changes with it.
        </p>
      </section>

      <section className="settings__group">
        <h3 className="settings__group-title">Keyboard</h3>
        <dl className="settings__keys">
          {KEYS.map(([chord, what]) => (
            <div key={chord}>
              <dt>
                <kbd>{chord}</kbd>
              </dt>
              <dd>{what}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="settings__group">
        <h3 className="settings__group-title">Agent Computer</h3>
        <p className="settings__note">
          The Agent Computer stays closed until you open it from a conversation. It is a Linux
          desktop beside the chat — not a second messenger. Each teammate has their own machine and
          nothing on it is shared, so what one bot signs into is invisible to the others. Anything
          it does that sends, spends or deletes still waits for you in Approvals.
        </p>
      </section>
    </div>
  )
}
