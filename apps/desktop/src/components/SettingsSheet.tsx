/**
 * Everything that is not the conversation, behind one button.
 *
 * The shell used to put nine sections in a left rail, eight of which are
 * configuration: Approvals, Work, Integrations, Routines, Usage, Audit,
 * Knowledge, Builder. That made the product read as an admin console with a
 * chat tab bolted on, and it cost the sidebar to the tabs — which is why a
 * *conversation* was not addressable and group threads were nearly impossible
 * to find again.
 *
 * So they live here. A sheet over the app, a nav on the left, one section at a
 * time, `Esc` to leave. The order is deliberate: the four sections a person
 * touches while working (connection, models, what is waiting on them, what
 * their bots are plugged into) come before the three they touch occasionally,
 * and the panels that are really read-only history sit last under their own
 * heading rather than being promoted to the top level.
 *
 * The panels themselves are unchanged — this is composition, not a rewrite.
 * `ApprovalsPanel`, `IntegrationsPanel`, `RoutinesPanel`, `UsagePanel`,
 * `WorkPanel`, `AuditPanel`, `KnowledgePanel` and `BuilderPanel` are the same
 * components the rail used to mount.
 */
import { useEffect, useMemo, useRef, useState } from "react"
import { cx } from "../lib/format"
import type { ApprovalsApi } from "../hooks/useApprovals"
import type { BotsApi } from "../hooks/useBots"
import { ApprovalsPanel } from "./ApprovalsPanel"
import { AuditPanel } from "./AuditPanel"
import { BuilderPanel } from "./BuilderPanel"
import { ErrorBoundary } from "./ErrorBoundary"
import { GeneralSettings } from "./GeneralSettings"
import { Icon, type IconName } from "./Icon"
import { IntegrationsPanel } from "./IntegrationsPanel"
import { KnowledgePanel } from "./KnowledgePanel"
import { ModelsPanel } from "./ModelsPanel"
import { RoutinesPanel } from "./RoutinesPanel"
import { UsagePanel } from "./UsagePanel"
import { WorkPanel } from "./WorkPanel"

export type SettingsSection =
  | "general"
  | "models"
  | "approvals"
  | "connectors"
  | "routines"
  | "usage"
  | "profile"
  | "work"
  | "audit"
  | "knowledge"

interface SectionMeta {
  id: SettingsSection
  label: string
  glyph: IconName
  /** Matched by the "Find a setting" box alongside the label. */
  keywords: string
  group: "settings" | "records"
}

const SECTIONS: SectionMeta[] = [
  { id: "general", label: "General", glyph: "sliders", keywords: "api url endpoint theme dark light keyboard shortcuts computer desktop setup wizard connection", group: "settings" },
  { id: "models", label: "Models", glyph: "spark", keywords: "provider key azure openai anthropic google ollama credential routing pin tier", group: "settings" },
  { id: "approvals", label: "Approvals", glyph: "shield", keywords: "send spend delete pending waiting risk gate standing", group: "settings" },
  { id: "connectors", label: "Connectors", glyph: "plug", keywords: "integrations mcp secret binding oauth crm mail graph", group: "settings" },
  { id: "routines", label: "Routines", glyph: "repeat", keywords: "schedule cron automation recorder steps", group: "settings" },
  { id: "usage", label: "Usage", glyph: "chart", keywords: "spend cost budget ledger today tokens", group: "settings" },
  { id: "profile", label: "Profile", glyph: "user", keywords: "builder bot persona email voice signature prompt standing job create delete teammate memories", group: "settings" },
  { id: "work", label: "Work", glyph: "blocks", keywords: "work items handoff transfers queue", group: "records" },
  { id: "audit", label: "Audit", glyph: "list", keywords: "events history log actor", group: "records" },
  { id: "knowledge", label: "Knowledge", glyph: "book", keywords: "kb articles rag search", group: "records" },
]

export interface SettingsSheetProps {
  open: boolean
  section: SettingsSection
  onSection: (section: SettingsSection) => void
  onClose: () => void
  bots: BotsApi
  approvals: ApprovalsApi
  activeBotId: string | null
  /** `null` clears the selection — `BuilderPanel`'s picker has an empty option. */
  onSelectBot: (botId: string | null) => void
  usageRefreshKey: number
}

export function SettingsSheet({
  open,
  section,
  onSection,
  onClose,
  bots,
  approvals,
  activeBotId,
  onSelectBot,
  usageRefreshKey,
}: SettingsSheetProps) {
  const [query, setQuery] = useState("")
  const searchRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (!open) {
      setQuery("")
      return
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        onClose()
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open, onClose])

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return SECTIONS
    return SECTIONS.filter(
      (entry) =>
        entry.label.toLowerCase().includes(needle) || entry.keywords.includes(needle),
    )
  }, [query])

  if (!open) return null

  const active = SECTIONS.find((entry) => entry.id === section) ?? SECTIONS[0]

  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-label="Settings">
      {/*
        A backdrop that closes on click, and a panel that does not — the
        `stopPropagation` is on the panel rather than a `!contains` check
        because the sheet mounts selects and popovers whose own nodes leave
        the DOM on click.
      */}
      <button
        type="button"
        className="sheet__backdrop"
        aria-label="Close settings"
        onClick={onClose}
      />
      <div className="sheet__panel" onClick={(event) => event.stopPropagation()}>
        <nav className="sheet__nav" aria-label="Settings sections">
          {(["settings", "records"] as const).map((group) => {
            const entries = matches.filter((entry) => entry.group === group)
            if (entries.length === 0) return null
            return (
              <div key={group} className="sheet__nav-group">
                {group === "records" ? (
                  <div className="sheet__nav-label">Records</div>
                ) : null}
                {entries.map((entry) => (
                  <button
                    key={entry.id}
                    type="button"
                    className={cx("sheet__nav-item", entry.id === section && "sheet__nav-item--active")}
                    aria-current={entry.id === section}
                    onClick={() => onSection(entry.id)}
                  >
                    <Icon name={entry.glyph} size={15} />
                    <span>{entry.label}</span>
                    {entry.id === "approvals" && approvals.pendingCount > 0 ? (
                      <span className="badge">{approvals.pendingCount}</span>
                    ) : null}
                  </button>
                ))}
              </div>
            )
          })}
          {matches.length === 0 ? <p className="sheet__nav-empty">No setting matches that.</p> : null}
        </nav>

        <div className="sheet__main">
          <header className="sheet__header">
            <h2 className="sheet__title">{active.label}</h2>
            <div className="sheet__search">
              <Icon name="search" size={14} />
              <input
                ref={searchRef}
                type="search"
                className="sheet__search-input"
                placeholder="Find a setting"
                value={query}
                aria-label="Find a setting"
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <button
              type="button"
              className="sheet__close"
              onClick={onClose}
              aria-label="Close settings"
            >
              <Icon name="close" size={16} />
            </button>
          </header>

          <div className="sheet__body">
            <ErrorBoundary label={active.label}>
              {section === "general" ? <GeneralSettings /> : null}
              {section === "models" ? <ModelsPanel bots={bots} /> : null}
              {section === "approvals" ? <ApprovalsPanel approvals={approvals} bots={bots.bots} /> : null}
              {section === "connectors" ? (
                <IntegrationsPanel bots={bots.bots} activeBotId={activeBotId} onSelectBot={onSelectBot} />
              ) : null}
              {section === "routines" ? (
                <RoutinesPanel bots={bots.bots} activeBotId={activeBotId} onSelectBot={onSelectBot} />
              ) : null}
              {section === "usage" ? <UsagePanel refreshKey={usageRefreshKey} /> : null}
              {section === "profile" ? (
                <BuilderPanel bots={bots} activeBotId={activeBotId} onSelectBot={onSelectBot} />
              ) : null}
              {section === "work" ? <WorkPanel bots={bots.bots} /> : null}
              {section === "audit" ? <AuditPanel bots={bots.bots} /> : null}
              {section === "knowledge" ? <KnowledgePanel /> : null}
            </ErrorBoundary>
          </div>
        </div>
      </div>
    </div>
  )
}
