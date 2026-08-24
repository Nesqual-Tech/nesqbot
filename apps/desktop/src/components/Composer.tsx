import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react"
import { Spinner } from "./Spinner"

export interface ComposerProps {
  disabled?: boolean
  streaming?: boolean
  placeholder: string
  /** Changing this refocuses the textarea (e.g. when the thread changes). */
  focusKey?: string | null
  onSend: (text: string) => Promise<{ ok: boolean; error?: string }>
  onStop: () => void
  hint?: string
}

const MAX_HEIGHT = 220

export function Composer({
  disabled = false,
  streaming = false,
  placeholder,
  focusKey,
  onSend,
  onStop,
  hint,
}: ComposerProps) {
  const [draft, setDraft] = useState("")
  const [sending, setSending] = useState(false)
  const ref = useRef<HTMLTextAreaElement | null>(null)

  // Auto-grow without a layout dependency.
  useLayoutEffect(() => {
    const node = ref.current
    if (!node) return
    node.style.height = "auto"
    node.style.height = `${Math.min(node.scrollHeight, MAX_HEIGHT)}px`
  }, [draft])

  useEffect(() => {
    if (!disabled) ref.current?.focus()
  }, [focusKey, disabled])

  const submit = async () => {
    const text = draft.trim()
    if (!text || disabled || sending) return
    setDraft("")
    setSending(true)
    try {
      const result = await onSend(text)
      if (!result.ok) setDraft(text) // give the user their words back
    } finally {
      setSending(false)
      ref.current?.focus()
    }
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void submit()
    }
    if (event.key === "Escape" && streaming) {
      event.preventDefault()
      onStop()
    }
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <label className="sr-only" htmlFor="composer-input">
        Message your teammate
      </label>
      <textarea
        id="composer-input"
        ref={ref}
        className="composer__input"
        rows={1}
        value={draft}
        placeholder={placeholder}
        disabled={disabled}
        aria-describedby="composer-hint"
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="composer__actions">
        {streaming ? (
          <button type="button" className="btn btn--danger" onClick={onStop} aria-label="Stop generating">
            Stop
          </button>
        ) : (
          <button type="submit" className="btn btn--primary" disabled={disabled || sending || !draft.trim()}>
            {sending ? <Spinner size="sm" label="Sending" inline /> : "Send"}
          </button>
        )}
      </div>
      <p className="composer__hint" id="composer-hint">
        {hint ?? "Enter to send · Shift+Enter for a new line · Esc stops a stream"}
      </p>
    </form>
  )
}
