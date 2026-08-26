import { useRef, type KeyboardEvent } from "react"
import { productName } from "@nesqbot/ui"
import { cx } from "../lib/format"
import { useTheme } from "../state/theme"
import type { PanelTab } from "../state/AppState"
import { AccountBox } from "./AccountBox"
import { BotList, type BotListProps } from "./BotList"
import { NesqualLockup } from "./Brand"
import { Icon, type IconName } from "./Icon"

export interface SidebarProps {
  tab: PanelTab
  onTabChange: (tab: PanelTab) => void
  /** Opens the command palette. Also reachable on Ctrl/Cmd+K. */
  onOpenPalette?: () => void
  pendingApprovals: number
  botList: BotListProps
  /** Rendered under the nav — connection state, request id, etc. */
  statusLine?: string
  statusTone?: "ok" | "warn" | "error"
}

const TABS: Array<{ id: PanelTab; label: string; glyph: IconName }> = [
  { id: "chat", label: "Chat", glyph: "chat" },
  { id: "approvals", label: "Approvals", glyph: "shield" },
  { id: "integrations", label: "Integrations", glyph: "plug" },
  { id: "routines", label: "Routines", glyph: "repeat" },
  { id: "usage", label: "Usage", glyph: "chart" },
  { id: "audit", label: "Audit", glyph: "list" },
  { id: "knowledge", label: "Knowledge", glyph: "book" },
  { id: "builder", label: "Builder", glyph: "blocks" },
]

export function Sidebar({
  tab,
  onTabChange,
  onOpenPalette,
  pendingApprovals,
  botList,
  statusLine,
  statusTone = "ok",
}: SidebarProps) {
  const { theme, toggleTheme } = useTheme()
  const navRefs = useRef<Array<HTMLButtonElement | null>>([])

  const onNavKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = TABS.findIndex((t) => t.id === tab)
    let next = index
    switch (event.key) {
      case "ArrowDown":
      case "ArrowRight":
        next = (index + 1) % TABS.length
        break
      case "ArrowUp":
      case "ArrowLeft":
        next = (index - 1 + TABS.length) % TABS.length
        break
      case "Home":
        next = 0
        break
      case "End":
        next = TABS.length - 1
        break
      default:
        return
    }
    event.preventDefault()
    onTabChange(TABS[next].id)
    navRefs.current[next]?.focus()
  }

  return (
    <aside className="sidebar">
      {/*
        The real artwork, not a text heading. `wordmark: "continuation"` is the
        lockup as Nesqual actually sets it — the mark *is* the N and "ESQUAL"
        runs on from it — and it is vector, so it stays sharp at any size and in
        either theme. The product name sits under it in the scale's caps step,
        tracked to roughly the logo tagline's +0.16em.
      */}
      <div className="brand">
        <h1 className="brand__lockup">
          <NesqualLockup size={20} title="Nesqual Tech" />
        </h1>
        <div className="brand__product">{productName}</div>
      </div>

      {/*
        The palette, advertised.

        A Ctrl+K palette that nobody knows about is a feature for the person who
        wrote it. This is the discovery surface: it reads as a search field, it
        prints its own shortcut, and it sits above the nav where a search field
        is expected — so the shortcut is learned by the people who click it
        first, which is everyone, once.
      */}
      {onOpenPalette ? (
        <button type="button" className="palette-cue" onClick={onOpenPalette}>
          <Icon name="search" size={15} />
          <span className="palette-cue__label">Jump to…</span>
          <kbd className="palette-cue__kbd">Ctrl K</kbd>
        </button>
      ) : null}

      <div className="nav" role="tablist" aria-label="Workspace sections" onKeyDown={onNavKeyDown}>
        {TABS.map((item, index) => {
          const selected = tab === item.id
          const showBadge = item.id === "approvals" && pendingApprovals > 0
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`nav-tab-${item.id}`}
              aria-selected={selected}
              aria-controls={`panel-${item.id}`}
              tabIndex={selected ? 0 : -1}
              ref={(node) => {
                navRefs.current[index] = node
              }}
              className={cx("nav__item", selected && "nav__item--active")}
              onClick={() => onTabChange(item.id)}
            >
              <span className="nav__glyph">
                <Icon name={item.glyph} size={17} />
              </span>
              <span className="nav__label">{item.label}</span>
              {showBadge ? (
                <span className="badge" aria-label={`${pendingApprovals} pending approvals`}>
                  {pendingApprovals > 99 ? "99+" : pendingApprovals}
                </span>
              ) : null}
            </button>
          )
        })}
      </div>

      <div className="sidebar__section-label" id="sidebar-bots-label">
        Teammates
      </div>
      <BotList {...botList} />

      <div className="sidebar__footer">
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={toggleTheme}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        >
          <Icon name={theme === "dark" ? "sun" : "moon"} size={15} />
          <span>{theme === "dark" ? "Light" : "Dark"}</span>
        </button>
        {statusLine ? (
          <span className={cx("status-line", `status-line--${statusTone}`)} role="status">
            <span className="status-dot" aria-hidden="true" />
            {statusLine}
          </span>
        ) : null}
        <AccountBox />
      </div>
    </aside>
  )
}
