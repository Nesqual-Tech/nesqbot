import type { ReactNode } from "react"
import { errorMessage } from "../api/client"
import { cx } from "../lib/format"
import { NesqualWatermark } from "./Brand"
import { Icon, type IconName } from "./Icon"

export interface EmptyStateProps {
  title: string
  description?: ReactNode
  /**
   * Icon from the app's own set — decorative only. Was an emoji string, which
   * rendered as a different picture on every OS, ignored the palette entirely
   * (so it stayed full-colour in both themes) and turned to mush at 14px.
   */
  glyph?: IconName
  /**
   * Put the Nesqual mark behind the state. For the one empty surface that owns
   * a whole pane — never on a compact state inside a list.
   */
  watermark?: boolean
  tone?: "neutral" | "error" | "warning"
  actionLabel?: string
  onAction?: () => void
  children?: ReactNode
  compact?: boolean
}

export function EmptyState({
  title,
  description,
  glyph,
  tone = "neutral",
  actionLabel,
  onAction,
  children,
  compact = false,
  watermark = false,
}: EmptyStateProps) {
  return (
    <div
      className={cx("empty-state", `empty-state--${tone}`, compact && "empty-state--compact")}
      role={tone === "error" ? "alert" : undefined}
    >
      {/*
        One mark per state. Where the brand watermark is showing, the icon tile
        would be a second logo stacked on the first; the watermark is already
        doing that job and doing it more quietly.
      */}
      {watermark && !compact ? <NesqualWatermark size={80} /> : null}
      {glyph && !(watermark && !compact) ? (
        <div className="empty-state__glyph">
          <Icon name={glyph} size={22} />
        </div>
      ) : null}
      <h3 className="empty-state__title">{title}</h3>
      {description ? <div className="empty-state__body">{description}</div> : null}
      {children}
      {actionLabel && onAction ? (
        <button type="button" className="btn btn--ghost" onClick={onAction}>
          {actionLabel}
        </button>
      ) : null}
    </div>
  )
}

export interface ErrorStateProps {
  error: unknown
  title?: string
  onRetry?: () => void
  compact?: boolean
}

/** The standard "the API is unhappy" surface. Never a white screen. */
export function ErrorState({ error, title = "That did not load", onRetry, compact }: ErrorStateProps) {
  return (
    <EmptyState
      tone="error"
      glyph="alert"
      title={title}
      description={errorMessage(error)}
      actionLabel={onRetry ? "Try again" : undefined}
      onAction={onRetry}
      compact={compact}
    />
  )
}
