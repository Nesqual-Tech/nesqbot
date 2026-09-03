import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react"
import {
  applyMention,
  matchCandidates,
  mentionQuery,
  mentionedBotIds,
  type MentionCandidate,
} from "../lib/mentions"
import { BotAvatar } from "./BotAvatar"
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
  /**
   * Everyone this person can address, best first. The hint has always promised
   * `@`; this is what makes it true. Empty disables the picker but not the
   * parsing — see `lib/mentions`.
   */
  mentionCandidates?: MentionCandidate[]
  onSend: (text: string, mentionBotIds: string[]) => Promise<{ ok: boolean; error?: string }>
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
  mentionCandidates = [],
  onSend,
  onStop,
  hint,
}: ComposerProps) {
  const [draft, setDraft] = useState("")
  const [sending, setSending] = useState(false)
  const [caret, setCaret] = useState(0)
  // Dismissing is per-mention, not global: closing the list on `@sal` must not
  // keep it shut for the next `@` in the same message.
  const [dismissedAt, setDismissedAt] = useState<number | null>(null)
  const [highlight, setHighlight] = useState(0)
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

  const query = useMemo(() => mentionQuery(draft, caret), [draft, caret])
  const suggestions = useMemo(
    () => (query && mentionCandidates.length ? matchCandidates(query.query, mentionCandidates) : []),
    [query, mentionCandidates],
  )
  const menuOpen = suggestions.length > 0 && query !== null && dismissedAt !== query.start

  useEffect(() => {
    setHighlight(0)
  }, [query?.start, query?.query])

  const setText = (text: string, nextCaret: number) => {
    setDraft(text)
    setCaret(nextCaret)
    const node = ref.current
    if (!node) return
    // The value has not rendered yet, so the caret has to be placed after it has.
    requestAnimationFrame(() => {
      node.focus()
      node.setSelectionRange(nextCaret, nextCaret)
    })
  }

  const choose = (bot: MentionCandidate) => {
    if (!query) return
    const next = applyMention(draft, query, caret, bot)
    setText(next.text, next.caret)
    // A completed mention is still, textually, a mention being typed — the
    // caret sits right after "@Sales " and the query is "Sales ". Without this
    // the list reopens on top of the name that was just chosen. Marked done
    // rather than closed globally, so the next `@` still opens one.
    setDismissedAt(query.start)
  }

  const submit = async () => {
    const text = draft.trim()
    if (!text || disabled || sending) return
    setDraft("")
    setSending(true)
    try {
      const result = await onSend(text, mentionedBotIds(text, mentionCandidates))
      if (!result.ok) setDraft(text) // give the user their words back
    } finally {
      setSending(false)
      ref.current?.focus()
    }
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // The picker owns these keys while it is open, and only while it is open:
    // Enter still sends, and Escape still stops a stream, the moment it closes.
    if (menuOpen) {
      if (event.key === "ArrowDown") {
        event.preventDefault()
        setHighlight((i) => (i + 1) % suggestions.length)
        return
      }
      if (event.key === "ArrowUp") {
        event.preventDefault()
        setHighlight((i) => (i - 1 + suggestions.length) % suggestions.length)
        return
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault()
        choose(suggestions[highlight] ?? suggestions[0])
        return
      }
      if (event.key === "Escape") {
        event.preventDefault()
        setDismissedAt(query?.start ?? null)
        return
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void submit()
    }
    if (event.key === "Escape" && streaming) {
      event.preventDefault()
      onStop()
    }
  }

  const syncCaret = (node: HTMLTextAreaElement) => setCaret(node.selectionStart ?? node.value.length)

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      {menuOpen ? (
        <ul className="composer__mentions" role="listbox" aria-label="Mention a teammate">
          {suggestions.map((bot, index) => (
            <li key={bot.id}>
              <button
                type="button"
                role="option"
                aria-selected={index === highlight}
                className={`composer__mention${index === highlight ? " is-active" : ""}`}
                // `onMouseDown` rather than `onClick`: the click would blur the
                // textarea first, and the caret we are inserting at goes with it.
                onMouseDown={(event) => {
                  event.preventDefault()
                  choose(bot)
                }}
                onMouseEnter={() => setHighlight(index)}
              >
                <BotAvatar bot={bot} size={20} />
                <span className="composer__mention-name">{bot.name}</span>
                <span className="composer__mention-slug">@{bot.slug}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
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
        aria-expanded={menuOpen}
        aria-autocomplete="list"
        onChange={(event) => {
          setDraft(event.target.value)
          syncCaret(event.target)
        }}
        onKeyUp={(event) => syncCaret(event.currentTarget)}
        onClick={(event) => syncCaret(event.currentTarget)}
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
