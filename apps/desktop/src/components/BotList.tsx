import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react"
import { botColors, logoInk } from "@nesqbot/ui"
import { cx, initials, usd } from "../lib/format"
import { dur, ease, gsap, stagger, useGSAP } from "../lib/motion"
import { EmptyState, ErrorState } from "./EmptyState"
import { SkeletonList } from "./Skeleton"
import type { Bot } from "../types"

export interface BotListProps {
  bots: Bot[]
  loading: boolean
  error: unknown
  activeBotId: string | null
  onSelect: (bot: Bot) => void
  onRetry: () => void
  /** Optional per-bot spend, shown as a subtle right-hand hint. */
  spendByBot?: Record<string, { spent: number; budget: number }>
}

/**
 * Roving-tabindex listbox: one tab stop, arrow keys move, Home/End jump,
 * Enter/Space open the thread.
 */
export function BotList({ bots, loading, error, activeBotId, onSelect, onRetry, spendByBot }: BotListProps) {
  const [focusIndex, setFocusIndex] = useState(0)
  const itemRefs = useRef<Array<HTMLLIElement | null>>([])
  const listRef = useRef<HTMLUListElement | null>(null)
  const landed = useRef(false)

  /*
   * The teammate list lands once, when it first arrives from the API.
   *
   * Once, and not again: `landed` means adding a bot in the Builder later does
   * not re-animate the five that were already there. A list that restages
   * itself every time it changes is the difference between polish and fidget.
   */
  useGSAP(
    () => {
      if (landed.current || bots.length === 0) return
      landed.current = true
      gsap.from(".bot-item", {
        y: 6,
        autoAlpha: 0,
        duration: dur("base"),
        ease: ease("entrance"),
        stagger: stagger(0.035),
      })
    },
    { dependencies: [bots.length], scope: listRef },
  )

  useEffect(() => {
    const active = bots.findIndex((b) => b.id === activeBotId)
    if (active >= 0) setFocusIndex(active)
  }, [activeBotId, bots])

  if (loading && bots.length === 0) {
    return (
      <div className="bot-list" aria-busy="true">
        <SkeletonList rows={5} />
      </div>
    )
  }

  if (error && bots.length === 0) {
    return (
      <div className="bot-list">
        <ErrorState error={error} title="Teammates unavailable" onRetry={onRetry} compact />
      </div>
    )
  }

  if (bots.length === 0) {
    return (
      <div className="bot-list">
        <EmptyState
          compact
          glyph="bot"
          title="No teammates yet"
          description="Create one in the Builder panel to get started."
        />
      </div>
    )
  }

  const focusItem = (index: number) => {
    const clamped = Math.max(0, Math.min(bots.length - 1, index))
    setFocusIndex(clamped)
    itemRefs.current[clamped]?.focus()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault()
        focusItem(focusIndex + 1)
        break
      case "ArrowUp":
        event.preventDefault()
        focusItem(focusIndex - 1)
        break
      case "Home":
        event.preventDefault()
        focusItem(0)
        break
      case "End":
        event.preventDefault()
        focusItem(bots.length - 1)
        break
      case "Enter":
      case " ": {
        event.preventDefault()
        const bot = bots[focusIndex]
        if (bot) onSelect(bot)
        break
      }
      default:
        break
    }
  }

  return (
    <ul className="bot-list" ref={listRef} role="listbox" aria-label="Bot teammates" onKeyDown={onKeyDown}>
      {bots.map((bot, index) => {
        const spend = spendByBot?.[bot.id]
        const over = spend && spend.budget > 0 ? spend.spent / spend.budget : 0
        return (
          <li
            key={bot.id}
            id={`bot-option-${bot.id}`}
            ref={(node) => {
              itemRefs.current[index] = node
            }}
            role="option"
            aria-selected={bot.id === activeBotId}
            tabIndex={index === focusIndex ? 0 : -1}
            className={cx("bot-item", bot.id === activeBotId && "bot-item--active")}
            onClick={() => onSelect(bot)}
            onFocus={() => setFocusIndex(index)}
          >
            <span
              className="avatar"
              style={{ "--avatar-bg": botColors[bot.slug] || logoInk } as CSSProperties}
              aria-hidden="true"
            >
              {initials(bot.name)}
            </span>
            <span className="bot-item__meta">
              <span className="bot-item__name">{bot.name}</span>
              <span className="bot-item__role">{bot.role || (bot.is_system ? "System bot" : "Custom bot")}</span>
            </span>
            {spend ? (
              <span
                className={cx("bot-item__spend", over > 0.8 && "bot-item__spend--warn")}
                title={`${usd(spend.spent, true)} of ${usd(spend.budget)} today`}
              >
                {usd(spend.spent)}
              </span>
            ) : null}
          </li>
        )
      })}
    </ul>
  )
}
