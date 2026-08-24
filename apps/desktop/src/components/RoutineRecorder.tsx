/**
 * Records the real actions the operator performs on the takeover canvas and
 * turns them into a routine via `POST /routines/teach`. This replaces the old
 * `teachDemo()` button that posted canned steps.
 */
import { useState } from "react"
import { desktopActionRisk, requiresApproval } from "@nesqbot/protocol"
import { errorMessage } from "../api/client"
import * as api from "../api/endpoints"
import { toRecordedPayload } from "../hooks/useRoutines"
import { cx, isPlausibleCron } from "../lib/format"
import { useRecorder, useToast } from "../state/AppState"
import { Spinner } from "./Spinner"
import type { Bot, DesktopActionName } from "../types"

export interface RoutineRecorderProps {
  bot: Bot
  takeover: boolean
  onRequestTakeover: () => void
  /**
   * True while a human handoff is outstanding on this bot.
   *
   * `DesktopPane` stops feeding actions to the recorder for the duration —
   * a takeover is a sign-in, and a saved routine step reading
   * `type "hunter2"` is a password in the database. Silence would be worse
   * than the capture, though: someone who armed the recorder deliberately has
   * to be told why nothing is landing.
   */
  capturePaused?: boolean
}

const MANUAL_ACTIONS: Array<{ value: DesktopActionName; label: string; needsText: boolean }> = [
  { value: "open_chromium", label: "Open Chromium at URL", needsText: true },
  { value: "type", label: "Type text", needsText: true },
  { value: "key", label: "Press key", needsText: true },
  { value: "click", label: "Click at x,y", needsText: false },
]

export function RoutineRecorder({ bot, takeover, onRequestTakeover, capturePaused = false }: RoutineRecorderProps) {
  const recorder = useRecorder()
  const toast = useToast()

  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [cron, setCron] = useState("")
  const [saving, setSaving] = useState(false)
  const [manualAction, setManualAction] = useState<DesktopActionName>("open_chromium")
  const [manualText, setManualText] = useState("")
  const [manualX, setManualX] = useState("")
  const [manualY, setManualY] = useState("")

  const steps = recorder.botId === bot.id ? recorder.steps : []
  const cronValid = isPlausibleCron(cron)
  // Same keyword table the API classifies with, so a step that will park on an
  // approval says so before the routine ever runs. The server decides.
  const manualRisk = desktopActionRisk(manualAction)

  const addManualStep = () => {
    const spec = MANUAL_ACTIONS.find((a) => a.value === manualAction)
    if (spec?.needsText && !manualText.trim()) {
      toast.warning("That step needs a value", "Add the URL, text, or key first.")
      return
    }
    recorder.addStep({
      type: "desktop",
      action: manualAction,
      text: manualText.trim() || undefined,
      keys: manualAction === "key" && manualText.trim() ? [manualText.trim()] : undefined,
      x: manualX ? Number(manualX) : undefined,
      y: manualY ? Number(manualY) : undefined,
      label: `${manualAction}${manualText ? ` · ${manualText}` : ""}`,
    })
    setManualText("")
    setManualX("")
    setManualY("")
  }

  const save = async () => {
    if (!name.trim()) {
      toast.warning("Name the routine", "Give it something you will recognise in the list.")
      return
    }
    if (steps.length === 0) {
      toast.warning("Nothing recorded yet", "Record some desktop actions or add a step by hand.")
      return
    }
    if (!cronValid) {
      toast.warning("That cron does not look right", "Use 5 or 6 fields, e.g. 0 9 * * 1")
      return
    }
    setSaving(true)
    try {
      const routine = await api.teachRoutine({
        bot_id: bot.id,
        name: name.trim(),
        description: description.trim(),
        recorded_steps: toRecordedPayload(steps),
        schedule_cron: cron.trim() || null,
      })
      toast.success("Routine saved", `${routine.name} · ${steps.length} steps`)
      recorder.clear()
      setName("")
      setDescription("")
      setCron("")
    } catch (err) {
      toast.error("Could not save the routine", errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className={cx("recorder", recorder.recording && "recorder--live")}>
      <button
        type="button"
        className="recorder__toggle"
        aria-expanded={open}
        aria-controls="recorder-body"
        onClick={() => setOpen((prev) => !prev)}
      >
        <span className="recorder__title">
          {recorder.recording ? <span className="rec-dot" aria-hidden="true" /> : null}
          Routine recorder
        </span>
        <span className="recorder__count">{steps.length} steps</span>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>

      {open ? (
        <div className="recorder__body" id="recorder-body">
          <div className="recorder__row">
            <button
              type="button"
              className={cx("btn", "btn--sm", recorder.recording ? "btn--danger" : "btn--primary")}
              onClick={() => recorder.toggle(bot.id)}
              aria-pressed={recorder.recording}
            >
              {recorder.recording ? "Stop recording" : "Record"}
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={recorder.clear}
              disabled={steps.length === 0}
            >
              Clear
            </button>
            {recorder.recording && !takeover ? (
              <button type="button" className="btn btn--ghost btn--sm" onClick={onRequestTakeover}>
                Take over to capture
              </button>
            ) : null}
          </div>

          {recorder.recording ? (
            <p className={cx("recorder__hint", capturePaused && "recorder__hint--paused")} role="status">
              {capturePaused
                ? `Capture is paused while ${bot.name} is waiting on you. Sign-in keystrokes are never recorded into a routine.`
                : takeover
                  ? "Every click and keystroke on the canvas is being captured in order."
                  : "Recording is armed — take over the desktop so your actions are captured."}
            </p>
          ) : null}

          {steps.length === 0 ? (
            <p className="recorder__hint">Nothing recorded yet. Record live actions, or add a step by hand below.</p>
          ) : (
            <ol className="recorder__steps">
              {steps.map((step, index) => (
                <li key={step.uid} className="recorder__step">
                  <span className="recorder__step-index">{index + 1}</span>
                  <input
                    className="input input--flush"
                    value={step.label ?? step.action}
                    aria-label={`Step ${index + 1} name`}
                    onChange={(event) => recorder.updateStep(step.uid, { label: event.target.value })}
                  />
                  <code className="recorder__step-action">
                    {step.action}
                    {typeof step.x === "number" ? ` ${step.x},${step.y ?? 0}` : ""}
                  </code>
                  {requiresApproval(desktopActionRisk(step.action)) ? (
                    <span className="chip chip--warn" title="This step will pause for approval when the routine runs">
                      approval
                    </span>
                  ) : null}
                  <span className="recorder__step-actions">
                    <button
                      type="button"
                      className="btn btn--ghost btn--xs"
                      onClick={() => recorder.moveStep(step.uid, -1)}
                      disabled={index === 0}
                      aria-label={`Move step ${index + 1} up`}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost btn--xs"
                      onClick={() => recorder.moveStep(step.uid, 1)}
                      disabled={index === steps.length - 1}
                      aria-label={`Move step ${index + 1} down`}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className="btn btn--ghost btn--xs"
                      onClick={() => recorder.removeStep(step.uid)}
                      aria-label={`Delete step ${index + 1}`}
                    >
                      ✕
                    </button>
                  </span>
                </li>
              ))}
            </ol>
          )}

          <fieldset className="recorder__manual">
            <legend>Add a step by hand</legend>
            <select
              className="select"
              value={manualAction}
              onChange={(event) => setManualAction(event.target.value)}
              aria-label="Manual step action"
            >
              {MANUAL_ACTIONS.map((action) => (
                <option key={action.value} value={action.value}>
                  {action.label}
                </option>
              ))}
            </select>
            <input
              className="input"
              placeholder="value"
              value={manualText}
              aria-label="Manual step value"
              onChange={(event) => setManualText(event.target.value)}
            />
            <input
              className="input input--tiny"
              placeholder="x"
              inputMode="numeric"
              value={manualX}
              aria-label="Manual step x"
              onChange={(event) => setManualX(event.target.value)}
            />
            <input
              className="input input--tiny"
              placeholder="y"
              inputMode="numeric"
              value={manualY}
              aria-label="Manual step y"
              onChange={(event) => setManualY(event.target.value)}
            />
            <button type="button" className="btn btn--ghost btn--sm" onClick={addManualStep}>
              Add
            </button>
            {requiresApproval(manualRisk) ? (
              <span className="chip chip--warn">{manualRisk} · will need approval</span>
            ) : null}
          </fieldset>

          <div className="recorder__save">
            <label className="field">
              <span className="field__label">Routine name</span>
              <input
                className="input"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={`${bot.name} morning sweep`}
              />
            </label>
            <label className="field">
              <span className="field__label">Description</span>
              <input
                className="input"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="What this routine is for"
              />
            </label>
            <label className="field">
              <span className="field__label">Schedule (cron, optional)</span>
              <input
                className={cx("input", !cronValid && "input--invalid")}
                value={cron}
                onChange={(event) => setCron(event.target.value)}
                placeholder="0 9 * * 1"
                aria-invalid={!cronValid}
                aria-describedby="cron-help"
              />
              <span className="field__help" id="cron-help">
                {cronValid ? "Leave empty to run only on demand." : "Expected 5 or 6 space-separated fields."}
              </span>
            </label>
            <button
              type="button"
              className="btn btn--primary btn--sm"
              onClick={() => void save()}
              disabled={saving || steps.length === 0}
            >
              {saving ? <Spinner inline label="Saving" /> : "Save routine"}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
