/**
 * Turning a stream ticket into a noVNC URL the webview can actually load.
 *
 * A Bot Desktop lives on a private address inside the Azure VNet and has no
 * public IP — that per-bot isolation is the product's headline claim, so it is
 * not going to grow one. The desktop is therefore reached through the API's
 * stream proxy, and this module builds the two URLs that flow from one ticket:
 *
 *   iframe src   `<API base>/bots/<id>/desktop/stream/<ticket>/vnc.html?…`
 *   noVNC socket `<API base>/bots/<id>/desktop/stream/<ticket>/websockify`
 *
 * The second one is the fiddly half. noVNC does **not** resolve its WebSocket
 * URL relative to the page it was loaded from: `app/ui.js` builds
 * `ws://<host>/<path>` where `path` defaults to `websockify`, i.e. always from
 * the web root. Behind a proxy that means the socket has to be handed over
 * explicitly, as a root-relative path with no leading slash — which is what
 * `websockifyPath` computes and passes as noVNC's `path` config var.
 */
import { API_BASE } from "../api/client"
import type { DesktopStreamTicket } from "../types"

/** The API base as an absolute URL, whatever form `VITE_API_URL` took. */
function apiUrl(): URL {
  // A relative `VITE_API_URL` (e.g. "/api") is legal, so resolve against the
  // document rather than assuming the base is absolute.
  return new URL(API_BASE, window.location.href)
}

/**
 * The proxied WebSocket path as noVNC wants it: from the web root, no leading
 * slash, including the API's own mount prefix.
 */
export function websockifyPath(ticket: DesktopStreamTicket): string {
  const base = apiUrl()
  const full = `${base.pathname.replace(/\/+$/, "")}${ticket.ws_path}`
  return full.replace(/^\/+/, "")
}

/**
 * The full `src` for the viewer iframe.
 *
 * `autoconnect` because the user already asked to see the desktop by opening
 * the pane; `reconnect=0` because a reconnect would reuse a ticket the first
 * connection already burned, and failing visibly beats retrying invisibly;
 * `resize=scale` so a 1280×800 desktop fits whatever the pane is.
 */
export function desktopStreamUrl(ticket: DesktopStreamTicket): string {
  const base = apiUrl()
  const url = new URL(`${base.pathname.replace(/\/+$/, "")}${ticket.stream_path}`, base)
  url.searchParams.set("autoconnect", "1")
  url.searchParams.set("reconnect", "0")
  url.searchParams.set("resize", "scale")
  url.searchParams.set("path", websockifyPath(ticket))
  if (ticket.vnc_password) url.searchParams.set("password", ticket.vnc_password)
  return url.toString()
}

/** The API origin, for diagnosing a CSP `frame-src` that has not been widened. */
export function apiOrigin(): string {
  return apiUrl().origin
}
