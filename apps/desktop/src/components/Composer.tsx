import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"

export interface ComposerProps {
  disabled?: boolean
  streaming?: boolean
  placeholder: string
  /** Changing this refocuses the textarea (e.g. when the thread changes). */
  focusKey?: string | null
  /**
   * Text dropped into the box from outside — a suggested prompt on an empty
   * thread. Deliberately *not* sent on click: the suggestions are a starting
   * register, and the useful thing is usually the one you edit before
   * sending. The `key` is what makes picking the same suggestion twice work.
   */
  prefill?: { text: string; key: number } | null
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
  prefill,
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

  useEffect(() => {
    if (!prefill) return
    setDraft(prefill.text)
    const node = ref.current
    if (!node) return
    node.focus()
    // Caret at the end, not over the text: a selected suggestion that the next
    // keystroke wipes out is worse than no suggestion.
    requestAnimationFrame(() => node.setSelectionRange(node.value.length, node.value.length))
  }, [prefill])

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
          <button
            type="submit"
            className="composer__send"
            disabled={disabled || sending || !draft.trim()}
            aria-label="Send"
            title="Send (Enter)"
          >
            {sending ? <Spinner size="sm" label="Sending" inline /> : <Icon name="send" size={16} />}
          </button>
        )}
      </div>
      <p className="composer__hint" id="composer-hint">
        {hint ?? "Enter to send. Shift+Enter for a new line. Esc stops a stream."}
      </p>
    </form>
  )
}
