import { cx } from "../lib/format"

export interface SpinnerProps {
  size?: "sm" | "md"
  label?: string
  /** Renders the label next to the spinner instead of only for screen readers. */
  inline?: boolean
}

export function Spinner({ size = "sm", label = "Loading", inline = false }: SpinnerProps) {
  return (
    <span className={cx("spinner-wrap", inline && "spinner-wrap--inline")} role="status" aria-live="polite">
      <span className={cx("spinner", `spinner--${size}`)} aria-hidden="true" />
      <span className={inline ? "spinner-label" : "sr-only"}>{label}</span>
    </span>
  )
}
