import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from "react"
import { applyMention, matchCandidates, mentionQuery, mentionedBotIds, type MentionCandidate } from "../lib/mentions"
import {
  ACCEPT,
  AttachmentRejected,
  MAX_ATTACHMENTS,
  filesFrom,
  releaseStaged,
  stageFile,
  type StagedAttachment,
} from "../lib/attachments"
import { useToast } from "../state/AppState"
import { StagedAttachments } from "./Attachments"
import { BotAvatar } from "./BotAvatar"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"

export interface ComposerProps {
  disabled?: boolean
  streaming?: boolean
  placeholder: string
  /**
   * Changing this refocuses the textarea (e.g. when the thread changes) and
   * is the key the unsent draft is remembered under: switch away mid-sentence
   * and the sentence is there when you come back.
   */
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
  /**
   * What the person last sent on this thread. Arrow-up in an empty box puts it
   * back — the fastest way to fix a typo or ask the same thing of somebody
   * else, and a habit every terminal and every chat app has trained.
   */
  lastSent?: string | null
  onSend: (
    text: string,
    mentionBotIds: string[],
    attachments: StagedAttachment[],
  ) => Promise<{ ok: boolean; error?: string }>
  onStop: () => void
  hint?: string
}

const MAX_HEIGHT = 220
const DRAFT_PREFIX = "nesq.draft."

function readDraft(key: string | null | undefined): string {
  if (!key) return ""
  try {
    return localStorage.getItem(DRAFT_PREFIX + key) ?? ""
  } catch {
    return ""
  }
}

function writeDraft(key: string | null | undefined, text: string): void {
  if (!key) return
  try {
    if (text.trim()) localStorage.setItem(DRAFT_PREFIX + key, text)
    else localStorage.removeItem(DRAFT_PREFIX + key)
  } catch {
    // Storage full or blocked: the draft lives in memory until the switch.
  }
}

export function Composer({
  disabled = false,
  streaming = false,
  placeholder,
  focusKey,
  prefill,
  mentionCandidates = [],
  lastSent,
  onSend,
  onStop,
  hint,
}: ComposerProps) {
  const toast = useToast()
  const [draft, setDraft] = useState(() => readDraft(focusKey))
  const [sending, setSending] = useState(false)
  const [caret, setCaret] = useState(0)
  // Dismissing is per-mention, not global: closing the list on `@sal` must not
  // keep it shut for the next `@` in the same message.
  const [dismissedAt, setDismissedAt] = useState<number | null>(null)
  const [highlight, setHighlight] = useState(0)
  const [staged, setStaged] = useState<StagedAttachment[]>([])
  const [dragging, setDragging] = useState(false)
  const ref = useRef<HTMLTextAreaElement | null>(null)
  const fileInput = useRef<HTMLInputElement | null>(null)
  const draftKey = useRef(focusKey)

  // Auto-grow without a layout dependency.
  useLayoutEffect(() => {
    const node = ref.current
    if (!node) return
    node.style.height = "auto"
    node.style.height = `${Math.min(node.scrollHeight, MAX_HEIGHT)}px`
  }, [draft])

  /*
   * Drafts follow the thread. On a switch the outgoing thread's text is put
   * away under its own key and the incoming one's is taken out; the staged
   * files are dropped, because a screenshot meant for one conversation is
   * rarely meant for the next and the object URLs must be released anyway.
   */
  useEffect(() => {
    if (draftKey.current === focusKey) return
    writeDraft(draftKey.current, draft)
    draftKey.current = focusKey
    setDraft(readDraft(focusKey))
    setStaged((prev) => {
      prev.forEach(releaseStaged)
      return []
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusKey])

  useEffect(() => {
    const timer = setTimeout(() => writeDraft(draftKey.current, draft), 300)
    return () => clearTimeout(timer)
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

  /* ------------------------------------------------------------ files */

  const addFiles = useCallback(
    async (files: File[]) => {
      if (!files.length || disabled) return
      const room = MAX_ATTACHMENTS - staged.length
      if (room <= 0) {
        toast.warning("Attachment limit", `At most ${MAX_ATTACHMENTS} files per message.`)
        return
      }
      const accepted: StagedAttachment[] = []
      for (const file of files.slice(0, room)) {
        try {
          accepted.push(await stageFile(file))
        } catch (err) {
          toast.error("Not attached", err instanceof AttachmentRejected ? err.message : String(err))
        }
      }
      if (files.length > room) {
        toast.warning("Attachment limit", `Only the first ${room} of ${files.length} files were added.`)
      }
      if (accepted.length) setStaged((prev) => [...prev, ...accepted])
      ref.current?.focus()
    },
    [disabled, staged.length, toast],
  )

  const removeStaged = (uid: string) => {
    setStaged((prev) => {
      const gone = prev.find((item) => item.uid === uid)
      if (gone) releaseStaged(gone)
      return prev.filter((item) => item.uid !== uid)
    })
  }

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = filesFrom(event.clipboardData)
    if (!files.length) return
    // A pasted screenshot is a file with no text alongside it; a pasted
    // spreadsheet cell range is text *and* a file — keep the text, add the file.
    if (!event.clipboardData.getData("text/plain")) event.preventDefault()
    void addFiles(files)
  }

  const onDrop = (event: DragEvent<HTMLFormElement>) => {
    event.preventDefault()
    setDragging(false)
    void addFiles(filesFrom(event.dataTransfer))
  }

  const onDragOver = (event: DragEvent<HTMLFormElement>) => {
    if (!Array.from(event.dataTransfer.types).includes("Files")) return
    event.preventDefault()
    if (!dragging) setDragging(true)
  }

  /* ------------------------------------------------------------- send */

  const submit = async () => {
    const text = draft.trim()
    if ((!text && staged.length === 0) || disabled || sending) return
    const files = staged
    setDraft("")
    setStaged([])
    writeDraft(draftKey.current, "")
    setSending(true)
    try {
      const result = await onSend(text, mentionedBotIds(text, mentionCandidates), files)
      if (!result.ok) {
        // give the user their words — and their files — back
        setDraft(text)
        setStaged(files)
      } else {
        // Previews outlive the send only as long as the optimistic bubble;
        // by the time the transcript refetches, the real bytes are fetched
        // through the API. Releasing here would break that bubble, so the
        // URLs are left to the page — a handful of object URLs is nothing.
      }
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
      return
    }
    if (event.key === "ArrowUp" && !draft && lastSent) {
      event.preventDefault()
      setText(lastSent, lastSent.length)
      return
    }
    if (event.key === "Escape" && streaming) {
      event.preventDefault()
      onStop()
    }
  }

  const syncCaret = (node: HTMLTextAreaElement) => setCaret(node.selectionStart ?? node.value.length)
  const canSend = !disabled && !sending && (Boolean(draft.trim()) || staged.length > 0)

  return (
    <form
      className={`composer${dragging ? " composer--dragging" : ""}`}
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
      onDragOver={onDragOver}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
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
      <StagedAttachments items={staged} onRemove={removeStaged} />
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
        onPaste={onPaste}
      />
      <div className="composer__actions">
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(event) => {
            void addFiles(Array.from(event.target.files ?? []))
            event.target.value = ""
          }}
        />
        <button
          type="button"
          className="composer__attach"
          onClick={() => fileInput.current?.click()}
          disabled={disabled || staged.length >= MAX_ATTACHMENTS}
          aria-label="Attach a file"
          title="Attach an image or a text file — or paste, or drop one here"
        >
          <Icon name="paperclip" size={16} />
        </button>
        {streaming ? (
          <button type="button" className="btn btn--danger" onClick={onStop} aria-label="Stop generating">
            Stop
          </button>
        ) : (
          <button type="submit" className="composer__send" disabled={!canSend} aria-label="Send" title="Send (Enter)">
            {sending ? <Spinner size="sm" label="Sending" inline /> : <Icon name="send" size={16} />}
          </button>
        )}
      </div>
      <p className="composer__hint" id="composer-hint">
        {dragging ? "Drop to attach" : (hint ?? "Enter to send. Shift+Enter for a new line. Esc stops a stream.")}
      </p>
    </form>
  )
}
