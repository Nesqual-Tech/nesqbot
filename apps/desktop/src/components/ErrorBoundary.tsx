import { Component, type ErrorInfo, type ReactNode } from "react"
import { NesqualWatermark } from "./Brand"
import { Icon } from "./Icon"

interface ErrorBoundaryProps {
  /** Shown in the fallback so the user knows which panel broke. */
  label: string
  children: ReactNode
  onReset?: () => void
  /**
   * `panel` (default) degrades one pane. `root` is the last line of defence
   * around the whole application — see the note below.
   */
  variant?: "panel" | "root"
}

interface ErrorBoundaryState {
  error: Error | null
  componentStack: string | null
  copied: boolean
}

/**
 * A render crash degrades to a message instead of a blank window.
 *
 * ## Why there is a `root` variant
 *
 * `App.tsx` wraps each panel's *contents* (Chat, Approvals, Integrations,
 * Routines, Usage, Builder, Bot Desktop), and for a long time that was all.
 * Everything else in the shell was unprotected: the `Sidebar`, the composer,
 * the toast viewport, the takeover beacon, and `Shell` itself — which runs a
 * `useGSAP` on every tab change. A throw in any of those unmounted the entire
 * React tree, leaving an empty `<body>`.
 *
 * That is not a hypothetical. `TakeoverBeacon` called a hook after an early
 * return; the first time a run parked on a human, React raised "Rendered more
 * hooks than during the previous render", no boundary was above it, and the
 * installed app went white. The user's whole bug report could only be "it
 * returns an empty screen", because that is genuinely all there was to see.
 *
 * So: the hook bug is fixed at source, and this exists so that the *next* one
 * costs a legible error screen instead of the product.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, componentStack: null, copied: false }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ componentStack: info.componentStack ?? null })
    // eslint-disable-next-line no-console
    console.error(`[nesqbot] ${this.props.label} crashed`, error, info.componentStack)
  }

  handleReset = (): void => {
    this.setState({ error: null, componentStack: null, copied: false })
    this.props.onReset?.()
  }

  /**
   * Everything a support ticket needs, as one blob.
   *
   * A blank screen is unreportable — the person can only say "it is empty".
   * A crash screen with a copy button turns the same incident into a stack
   * trace in a message, which is the difference between a fixable report and
   * a guess.
   */
  details(): string {
    const { error, componentStack } = this.state
    return [
      `Nesq Bot — ${this.props.label} crashed`,
      `When:  ${new Date().toISOString()}`,
      `Agent: ${typeof navigator === "undefined" ? "unknown" : navigator.userAgent}`,
      "",
      `${error?.name ?? "Error"}: ${error?.message ?? "Unknown error"}`,
      "",
      error?.stack ?? "(no stack)",
      "",
      "Component stack:",
      componentStack ?? "(none)",
    ].join("\n")
  }

  handleCopy = (): void => {
    void navigator.clipboard
      .writeText(this.details())
      .then(() => {
        this.setState({ copied: true })
        setTimeout(() => this.setState({ copied: false }), 2000)
      })
      .catch(() => this.setState({ copied: false }))
  }

  render(): ReactNode {
    const { error, copied } = this.state
    if (!error) return this.props.children

    const root = this.props.variant === "root"
    const message = error.message || "Unexpected client error."

    if (!root) {
      return (
        <div className="empty-state empty-state--error" role="alert">
          <div className="empty-state__glyph" aria-hidden="true">
            <Icon name="alert" size={22} />
          </div>
          <h3 className="empty-state__title">{this.props.label} hit an error</h3>
          <div className="empty-state__body">{message}</div>
          <button type="button" className="btn btn--ghost" onClick={this.handleReset}>
            Reload this panel
          </button>
        </div>
      )
    }

    return (
      <div className="crash" role="alert">
        <div className="crash__card">
          <NesqualWatermark size={72} />
          <div className="eyebrow">Nesq Bot</div>
          <h1 className="crash__title">The workspace stopped</h1>
          <p className="crash__body">
            Something in the interface threw an error and the app could not carry on drawing. Your bots are unaffected —
            they run in Azure and keep going whether this window is open or not. Nothing has been approved, sent or
            spent because of this.
          </p>
          <pre className="crash__detail">{message}</pre>
          <div className="crash__actions">
            <button type="button" className="btn btn--primary" onClick={() => location.reload()}>
              <Icon name="repeat" size={15} />
              Reload the app
            </button>
            <button type="button" className="btn btn--ghost" onClick={this.handleCopy}>
              <Icon name="copy" size={15} />
              {copied ? "Copied" : "Copy error details"}
            </button>
          </div>
          <p className="crash__hint">
            Copying the details and sending them to support is the fastest way to get this fixed.
          </p>
        </div>
      </div>
    )
  }
}
