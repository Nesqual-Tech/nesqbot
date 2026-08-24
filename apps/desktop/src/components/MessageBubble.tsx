import { memo, useState, type CSSProperties } from "react"
import { botColors, logoInk } from "@nesqbot/ui"
import { clockTime, cx, initials } from "../lib/format"
import { Markdown } from "./Markdown"
import type { Bot, Message } from "../types"

export interface MessageBubbleProps {
  message: Message
  bot?: Bot
  /** Renders the live caret while tokens are still arriving. */
  streaming?: boolean
}

/**
 * One message.
 *
 * ## Who gets Markdown
 *
 * Assistant messages do. The API has always written Markdown into
 * `message.content` — `_compose_desktop_reply` in `services/orchestrator.py`
 * emits `**bold**`, numbered lists and backticked call signatures — and this
 * component used to print it as source.
 *
 * User messages deliberately do not. Formatting what a person typed is
 * surprising on its own, and it would mean a pasted string could change how
 * their own message looks in their own transcript. What they typed is what
 * they see.
 *
 * Tool output does not either. It is a raw payload, it is already set in the
 * monospace face, and `white-space: pre-wrap` is the correct rendering for it.
 *
 * ## Why this is memoised
 *
 * The parse is cheap, but it is not free, and the transcript re-renders on
 * every streamed token (`ChatPane` maps the whole list). `memo` means a token
 * arriving in the live bubble re-renders and re-parses exactly one bubble —
 * the streaming one — instead of every message in the thread. That is the
 * whole of the anti-flicker story on the React side; the parser handles the
 * other half by keeping an unterminated fence a code block.
 */
export const MessageBubble = memo(function MessageBubble({ message, bot, streaming = false }: MessageBubbleProps) {
  const [copied, setCopied] = useState(false)
  const role = message.role === "user" ? "user" : message.role === "tool" ? "tool" : "assistant"
  const markdown = role === "assistant"
  /*
   * The bot's own colour, published to CSS as a custom property.
   *
   * `botColors` already gives every teammate an identity and the transcript
   * spent it on an 18px avatar chip. In a thread where three bots take turns —
   * which is the product's whole shape — that is not enough to tell at a glance
   * who is speaking. The same value now also draws a 2px rule down the leading
   * edge of the bubble, so the speaker is legible from the shape of the column
   * rather than from reading a name.
   */
  const accent = bot ? botColors[bot.slug] || logoInk : logoInk

  const copy = async () => {
    try {
      // The Markdown source, not the rendered text: what gets pasted elsewhere
      // should be what the bot actually wrote.
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }

  return (
    <article
      className={cx("bubble", `bubble--${role}`, streaming && "bubble--streaming")}
      style={{ "--bot-accent": accent } as CSSProperties}
    >
      {role !== "user" ? (
        <header className="bubble__header">
          <span className="avatar avatar--xs" style={{ "--avatar-bg": accent } as CSSProperties} aria-hidden="true">
            {initials(bot?.name ?? (role === "tool" ? "Tool" : "Bot"))}
          </span>
          <span className="bubble__author">{bot?.name ?? (role === "tool" ? "Tool output" : "Assistant")}</span>
          {message.created_at ? <time className="bubble__time">{clockTime(message.created_at)}</time> : null}
        </header>
      ) : null}

      <div className={cx("bubble__content", markdown && "bubble__content--md")}>
        {markdown ? <Markdown text={message.content} /> : message.content}
        {/*
          The caret. In Markdown mode it is a `::after` on the last rendered
          block (see `styles.css`), because a sibling element would land on its
          own line below a `<p>` or a `<ul>` instead of at the end of the text.
        */}
        {streaming && !markdown ? <span className="caret" aria-hidden="true" /> : null}
      </div>

      {message.content && !streaming ? (
        <button
          type="button"
          className="bubble__copy"
          onClick={() => void copy()}
          aria-label={copied ? "Copied to clipboard" : "Copy message"}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      ) : null}
    </article>
  )
})

/**
 * One bot handing work to another, drawn as an event in the transcript.
 *
 * ## Why this is a component and not a sentence
 *
 * "Bots that hand work to each other, with a recorded ledger" is one of the
 * two things this product is sold on. Until now the only trace of it in the
 * interface was whatever the bot happened to write in prose — Pike's message
 * ends "Handing the shortlist to Vesna" and nothing else on screen changed.
 * A reader had to take the transcript's word for it.
 *
 * The orchestrator already annotates the message: `meta.handoff_to` carries the
 * receiving bot's id. So the handoff is a fact the client has and was throwing
 * away. Rendering it as its own object — two identities, the direction between
 * them, and the ledger key it was written under — turns a claim in prose into
 * something the interface itself is asserting.
 *
 * The connector is drawn at 60.3 degrees. That is the mark's own diagonal (see
 * `packages/ui/src/logo.ts`, where every diagonal in the artwork measures
 * dx/dy = 1.75), reused as the one geometric motif this design allows itself.
 */
export interface HandoffRailProps {
  from?: Bot
  to?: Bot
  /** Ledger key the orchestrator recorded the handover under, when it gave one. */
  ledgerKey?: string
}

export function HandoffRail({ from, to, ledgerKey }: HandoffRailProps) {
  if (!from || !to) return null

  const label = `${from.name} handed this to ${to.name}`

  return (
    <div className="handoff" role="note" aria-label={label}>
      {/*
        Caption first. "Pike → Vesna handed over" put the verb after both
        nouns and read as though Vesna had done the handing; leading with it
        makes the row a labelled event — handed over, from, to.
      */}
      <span className="handoff__caption">handed over</span>

      <span className="handoff__party">
        <span
          className="avatar avatar--xs"
          style={{ "--avatar-bg": botColors[from.slug] || logoInk } as CSSProperties}
          aria-hidden="true"
        >
          {initials(from.name)}
        </span>
        <span className="handoff__name">{from.name}</span>
      </span>

      <span className="handoff__link" aria-hidden="true">
        <span className="handoff__slash" />
      </span>

      <span className="handoff__party">
        <span
          className="avatar avatar--xs"
          style={{ "--avatar-bg": botColors[to.slug] || logoInk } as CSSProperties}
          aria-hidden="true"
        >
          {initials(to.name)}
        </span>
        <span className="handoff__name">{to.name}</span>
      </span>

      {ledgerKey ? <code className="handoff__ledger">{ledgerKey}</code> : null}
    </div>
  )
}
