/**
 * The sidebar: conversations, not sections.
 *
 * What this replaces is a nine-item tab rail — Chat, Approvals, Work,
 * Integrations, Routines, Usage, Audit, Knowledge, Builder — with a list of
 * teammates underneath it. Two problems with that, both reported:
 *
 *  * it made the app read as an admin console with a chat tab, when the whole
 *    product is the conversation. Eight of those nine tabs are things you
 *    configure once and visit monthly; they now live behind the settings
 *    button in the footer, which is where a person looks for them anyway;
 *  * a *conversation* was not addressable. The rail selected a bot, and the
 *    thread you landed in was whichever one `ensureThreadForBot` picked, with
 *    a `<select>` in the chat header for the rest. So a group thread — the
 *    only way delegation reaches a teammate — was almost impossible to find
 *    again once you left it.
 *
 * So the list is threads. A row shows who is in it, what it is called, and
 * when it last moved; a group shows a stack of silhouettes and the names of
 * everybody in it. `+` starts a conversation with one teammate, the pair of
 * people starts one with several — which is the entire mechanism by which work
 * gets handed over, and it used to be unreachable from this app.
 */
import { useEffect, useMemo, useRef, useState } from "react"
import { productName } from "@nesqbot/ui"
import { searchMessages } from "../api/endpoints"
import { conversationTime, cx, truncate } from "../lib/format"
import { AccountBox } from "./AccountBox"
import { BotAvatar, BotAvatarStack } from "./BotAvatar"
import { NesqualLockup } from "./Brand"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"
import type { Bot, MessageSearchHit, Thread } from "../types"

export interface ConversationListProps {
  threads: Thread[]
  bots: Bot[]
  loading: boolean
  error: unknown
  activeThreadId: string | null
  onSelectThread: (thread: Thread) => void
  /** Start (or reopen) a one-to-one conversation with this teammate. */
  onStartWithBot: (bot: Bot) => void
  /** Start a conversation with several teammates seated from the first turn. */
  onStartGroup: (botIds: string[]) => void
  /** Footer: today's spend across all bots, already formatted. */
  spendLabel?: string
  desktopOpen: boolean
  onToggleDesktop: () => void
  onOpenSettings: () => void
  themeButton: React.ReactNode
}

export function ConversationList({
  threads,
  bots,
  loading,
  error,
  activeThreadId,
  onSelectThread,
  onStartWithBot,
  onStartGroup,
  spendLabel,
  desktopOpen,
  onToggleDesktop,
  onOpenSettings,
  themeButton,
}: ConversationListProps) {
  const [query, setQuery] = useState("")
  const [picker, setPicker] = useState<"none" | "bot" | "group">("none")
  const [groupPick, setGroupPick] = useState<string[]>([])
  const searchRef = useRef<HTMLInputElement | null>(null)
  /*
   * The server's answer to the same query: matches *inside* messages, across
   * every conversation. Title matching above is instant and local; this one
   * is debounced and only asked for two or more characters, and it lands
   * below the title matches so the list never jumps while you type.
   */
  const [hits, setHits] = useState<MessageSearchHit[]>([])
  const [searching, setSearching] = useState(false)
  useEffect(() => {
    const needle = query.trim()
    if (needle.length < 2) {
      setHits([])
      setSearching(false)
      return
    }
    const controller = new AbortController()
    const timer = setTimeout(() => {
      setSearching(true)
      searchMessages(needle, 20, controller.signal)
        .then((found) => setHits(found))
        .catch(() => setHits([]))
        .finally(() => setSearching(false))
    }, 250)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [query])

  const botById = useMemo(() => {
    const map: Record<string, Bot> = {}
    for (const bot of bots) map[bot.id] = bot
    return map
  }, [bots])

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return threads
      .map((thread) => {
        const seated = (thread.bot_ids ?? []).map((id) => botById[id]).filter(Boolean)
        return { thread, seated }
      })
      .filter(({ thread, seated }) => {
        if (!needle) return true
        const haystack = [thread.title, ...seated.map((bot) => `${bot.name} ${bot.role}`)].join(" ").toLowerCase()
        return haystack.includes(needle)
      })
  }, [threads, botById, query])

  const toggleGroupPick = (botId: string) => {
    setGroupPick((prev) => (prev.includes(botId) ? prev.filter((id) => id !== botId) : [...prev, botId]))
  }

  const closePickers = () => {
    setPicker("none")
    setGroupPick([])
  }

  return (
    <aside className="rail" onKeyDown={(event) => event.key === "Escape" && closePickers()}>
      <div className="brand">
        <h1 className="brand__lockup">
          <NesqualLockup size={20} title="Nesqual Tech" />
        </h1>
        <div className="brand__product">{productName}</div>
      </div>

      <div className="rail__tools">
        <div className="rail__search">
          <Icon name="search" size={14} />
          <input
            ref={searchRef}
            type="search"
            className="rail__search-input"
            placeholder="Search…"
            value={query}
            aria-label="Search conversations"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <button
          type="button"
          className={cx("rail__tool", picker === "bot" && "rail__tool--on")}
          onClick={() => setPicker((current) => (current === "bot" ? "none" : "bot"))}
          aria-expanded={picker === "bot"}
          aria-label="New conversation"
          title="New conversation"
        >
          <Icon name="plus" size={16} />
        </button>
        <button
          type="button"
          className={cx("rail__tool", picker === "group" && "rail__tool--on")}
          onClick={() => setPicker((current) => (current === "group" ? "none" : "group"))}
          aria-expanded={picker === "group"}
          aria-label="New group"
          title="New group"
        >
          <Icon name="users" size={16} />
        </button>
      </div>

      {picker === "bot" ? (
        <div className="rail__picker" role="menu" aria-label="Start a conversation">
          {bots.map((bot) => (
            <button
              key={bot.id}
              type="button"
              role="menuitem"
              className="rail__picker-row"
              onClick={() => {
                onStartWithBot(bot)
                closePickers()
              }}
            >
              <BotAvatar bot={bot} size={22} />
              <span className="rail__picker-name">{bot.name}</span>
              <span className="rail__picker-role">{truncate(bot.role, 28)}</span>
            </button>
          ))}
        </div>
      ) : null}

      {picker === "group" ? (
        <div className="rail__picker" aria-label="Start a group">
          {/*
            A group is for reading along, not for permission. `_delegate_targets`
            used to be "everyone else seated on this thread", which made the
            roster the difference between a chief of staff who could hand work
            over and one who could only file notes about it; it is the person's
            whole team now and a hand-off seats the recipient itself. Seating
            somebody up front is still worth doing — it is how they see the
            conversation from the start rather than only a brief.
          */}
          <p className="rail__picker-note">
            Pick everybody who should read along from the start. You do not need a group to get work handed over — a bot
            can reach anyone on your team and brings them in when it does.
          </p>
          {bots.map((bot) => (
            <label key={bot.id} className="rail__picker-row rail__picker-row--check">
              <input type="checkbox" checked={groupPick.includes(bot.id)} onChange={() => toggleGroupPick(bot.id)} />
              <BotAvatar bot={bot} size={22} />
              <span className="rail__picker-name">{bot.name}</span>
            </label>
          ))}
          <div className="rail__picker-actions">
            <button type="button" className="btn btn--ghost btn--sm" onClick={closePickers}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn--primary btn--sm"
              disabled={groupPick.length < 2}
              onClick={() => {
                onStartGroup(groupPick)
                closePickers()
              }}
            >
              Start group
            </button>
          </div>
        </div>
      ) : null}

      <div className="rail__list" role="list" aria-label="Conversations">
        {loading && rows.length === 0 ? <Spinner label="Loading conversations" /> : null}

        {!loading && rows.length === 0 && hits.length === 0 && !searching ? (
          <p className="rail__empty">
            {query ? "Nothing matches that." : "No conversations yet. Press + and pick a teammate."}
          </p>
        ) : null}

        {rows.map(({ thread, seated }) => {
          const group = seated.length > 1
          const primary = seated.find((bot) => bot.slug) ?? null
          return (
            <button
              key={thread.id}
              type="button"
              role="listitem"
              className={cx("convo", thread.id === activeThreadId && "convo--active")}
              onClick={() => onSelectThread(thread)}
            >
              {group ? (
                <BotAvatarStack bots={seated} size={24} className="convo__avatar" />
              ) : primary ? (
                <BotAvatar bot={primary} size={28} className="convo__avatar" />
              ) : (
                <span className="convo__avatar convo__avatar--none" aria-hidden="true" />
              )}
              <span className="convo__body">
                <span className="convo__title">
                  {thread.pinned ? (
                    <span className="convo__pin" aria-label="Pinned" title="Pinned">
                      <Icon name="pin" size={10} />
                    </span>
                  ) : null}
                  {truncate(thread.title || "Untitled", 28)}
                </span>
                {/*
                  A group's title is already the roster, so repeating the names
                  underneath it says nothing — the count does, and it is the
                  thing that distinguishes two groups with the same first two
                  members once the title has been truncated.
                */}
                <span className="convo__subtitle">
                  {group ? `Group · ${seated.length} teammates` : (primary?.role ?? "No teammate seated")}
                </span>
              </span>
              <span className="convo__stamp">{conversationTime(thread.updated_at)}</span>
            </button>
          )
        })}

        {error && rows.length === 0 ? (
          <p className="rail__empty rail__empty--error">
            Conversations could not be loaded. The API may be unreachable.
          </p>
        ) : null}

        {query.trim().length >= 2 ? (
          <div className="rail__hits" aria-label="Messages that match">
            <div className="rail__hits-label">
              In messages
              {searching ? <Spinner size="sm" label="Searching" inline /> : null}
            </div>
            {!searching && hits.length === 0 ? <p className="rail__empty">No message says that.</p> : null}
            {hits.map((hit) => {
              const thread = threads.find((t) => t.id === hit.thread_id)
              const speaker = hit.bot_id ? botById[hit.bot_id]?.name : "You"
              return (
                <button
                  key={hit.message_id}
                  type="button"
                  className="hit"
                  onClick={() => {
                    if (thread) onSelectThread(thread)
                  }}
                  disabled={!thread}
                  title={thread ? `Open ${hit.thread_title}` : "This conversation is no longer listed"}
                >
                  <span className="hit__meta">
                    <span className="hit__thread">{truncate(hit.thread_title || "Untitled", 26)}</span>
                    <span className="hit__stamp">{conversationTime(hit.created_at)}</span>
                  </span>
                  <span className="hit__snippet">
                    <span className="hit__speaker">{speaker ?? "Teammate"}:</span> {hit.snippet}
                  </span>
                </button>
              )
            })}
          </div>
        ) : null}
      </div>

      <div className="rail__footer">
        <div className="rail__footer-row">
          <span className="rail__spend" title="Spent today, all bots">
            {spendLabel ?? ""}
          </span>
          <button
            type="button"
            className={cx("rail__tool", desktopOpen && "rail__tool--on")}
            onClick={onToggleDesktop}
            aria-pressed={desktopOpen}
            aria-label="Agent Computer"
            title="Agent Computer (Ctrl ⇧ D)"
          >
            <Icon name="monitor" size={15} />
          </button>
          <button
            type="button"
            className="rail__tool"
            onClick={onOpenSettings}
            aria-label="Settings"
            title="Settings (Ctrl ,)"
          >
            <Icon name="sliders" size={15} />
          </button>
          {themeButton}
        </div>
        <AccountBox />
      </div>
    </aside>
  )
}
