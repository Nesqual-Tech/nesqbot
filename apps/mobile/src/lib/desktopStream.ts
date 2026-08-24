/**
 * Turning a stream ticket into a noVNC URL a `WebView` can actually load.
 *
 * A Bot Desktop lives on a private address inside the Azure VNet and has no
 * public IP — that per-bot isolation is the product's headline claim, so it is
 * not going to grow one. `BotDesktop.stream_url` is therefore a `10.60.x.x`
 * address that no phone can route to, on any network. **Pointing a WebView at
 * it cannot work**, which is what this app used to do: every session silently
 * failed over to the screenshot poller, and the live stream was dead code that
 * looked like a feature.
 *
 * The supported route is the API's own proxy, which already sits inside the
 * VNet. Because neither a WebView `source.uri` nor a WebSocket handshake can
 * carry an `Authorization` header, both legs authenticate with a short-lived
 * ticket that lives in the URL **path** — so noVNC's relative asset fetches
 * inherit it, which a query string would not survive.
 *
 * The fiddly half is the socket. noVNC does not resolve its WebSocket URL
 * relative to the page it was loaded from: `app/ui.js` builds
 * `ws://<host>/<path>` where `path` defaults to `websockify`, i.e. always from
 * the web root. Behind a proxy the socket therefore has to be handed over
 * explicitly, as a root-relative path with no leading slash, including the
 * API's own mount prefix. That is what {@link websockifyPath} computes.
 *
 * Every function here takes the API base URL as an argument rather than reading
 * it from the client module. That keeps the file free of any native import, so
 * `src/lib/__checks__/smoke.mjs` can load **this exact source** and assert the
 * URLs it builds — the alternative was a hand-copy of the logic in the test,
 * which is a test of the copy.
 */
import type { DesktopStreamTicket } from "../api/types"

/**
 * The API's mount prefix, e.g. `/api`, with no trailing slash.
 *
 * Derived from the base URL rather than hardcoded, because the base URL is
 * user-overridable from Settings and a staging deployment may mount elsewhere.
 * `ticket.stream_path` and `ticket.ws_path` are relative to the API root, not
 * to the host — the API does not guess its own public origin — so the prefix
 * has to be put back on by hand.
 */
function apiPrefix(base: string): string {
  const withoutScheme = base.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "")
  const slash = withoutScheme.indexOf("/")
  if (slash === -1) return ""
  return withoutScheme.slice(slash).replace(/\/+$/, "")
}

/** The API origin, e.g. `https://host:8080`. */
export function apiOrigin(base: string): string {
  const prefix = apiPrefix(base)
  return prefix ? base.slice(0, base.length - prefix.length) : base
}

/**
 * The proxied WebSocket path as noVNC wants it: from the web root, no leading
 * slash, including the API's mount prefix.
 */
export function websockifyPath(ticket: DesktopStreamTicket, base: string): string {
  return `${apiPrefix(base)}${ticket.ws_path}`.replace(/^\/+/, "")
}

/**
 * The full URL for the viewer WebView.
 *
 * - `autoconnect` because the person already asked to see the desktop by
 *   opening the screen; making them press Connect inside an embedded page is a
 *   second tap for nothing.
 * - `reconnect=0` because a reconnect would re-present a ticket the first
 *   connection already burned (`4409`), and failing visibly beats retrying
 *   invisibly forever.
 * - `resize=scale` so a 1280×800 desktop fits a 390pt-wide phone. `scale`
 *   rather than `remote`: asking the desktop to resize itself to phone
 *   dimensions would reflow the page the agent is working on, which is the one
 *   thing a viewer must never do.
 * - `view_only` when the person has not explicitly taken over, so a stray tap
 *   on a live browser session cannot click something.
 */
export function desktopStreamUrl(ticket: DesktopStreamTicket, options: { viewOnly?: boolean; base: string }): string {
  const { base } = options
  const params: string[] = [
    "autoconnect=1",
    "reconnect=0",
    "resize=scale",
    `path=${encodeURIComponent(websockifyPath(ticket, base))}`,
  ]
  if (options.viewOnly !== false) params.push("view_only=1")
  if (ticket.vnc_password) params.push(`password=${encodeURIComponent(ticket.vnc_password)}`)
  return `${base}${ticket.stream_path}?${params.join("&")}`
}

/**
 * Milliseconds until the ticket expires, floored at zero.
 *
 * Prefers the server's own `expires_in` over subtracting `expires_at` from the
 * phone's clock: a device whose clock is minutes out would otherwise decide a
 * perfectly good ticket had expired, or hold a dead one.
 */
export function ticketTtlMs(ticket: DesktopStreamTicket): number {
  if (typeof ticket.expires_in === "number" && Number.isFinite(ticket.expires_in)) {
    return Math.max(0, ticket.expires_in * 1000)
  }
  const at = Date.parse(ticket.expires_at)
  return Number.isNaN(at) ? 0 : Math.max(0, at - Date.now())
}
