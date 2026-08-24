/**
 * Thin bridge to the Tauri shell.
 *
 * The shell sets `withGlobalTauri: true`, so `window.__TAURI__` is available in
 * the packaged app and simply absent in a plain browser tab. Event subscription
 * degrades to a no-op when the app runs in the browser (`npm run dev`);
 * `invokeShell` throws instead, because a caller that needs a native command
 * needs to know it did not run rather than silently continue.
 */
import { invoke } from "@tauri-apps/api/core"

type UnlistenFn = () => void

interface TauriEventPayload<T> {
  event: string
  id: number
  payload: T
}

interface TauriGlobal {
  event?: {
    listen?: <T>(event: string, handler: (e: TauriEventPayload<T>) => void) => Promise<UnlistenFn>
    emit?: (event: string, payload?: unknown) => Promise<void>
  }
  window?: {
    getCurrentWindow?: () => { close?: () => Promise<void> }
  }
}

declare global {
  interface Window {
    __TAURI__?: TauriGlobal
  }
}

export const isTauri: boolean = typeof window !== "undefined" && Boolean(window.__TAURI__)

/** Thrown when a native command is called from a plain browser tab. */
export class ShellUnavailableError extends Error {
  constructor(command: string) {
    super(`"${command}" needs the Nesq Bot desktop shell; it is not available in a browser.`)
    this.name = "ShellUnavailableError"
  }
}

/**
 * Calls a command defined in `src-tauri/src/lib.rs`.
 *
 * Arguments and return values cross the IPC boundary as JSON. Callers that pass
 * a credential (see `auth/storage.ts`) rely on that boundary staying inside the
 * process — it is not a network hop — and on nothing here logging its payload.
 */
export async function invokeShell<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri) throw new ShellUnavailableError(command)
  return invoke<T>(command, args)
}

/**
 * Hand a URL to the operating system.
 *
 * This is what a link in model-produced Markdown does when it is clicked, and
 * the reason it exists is that the obvious alternative is a disaster: a plain
 * `<a href>` inside a Tauri webview *navigates the webview*. The app would be
 * replaced, in place, by whatever page a bot happened to quote — same window,
 * same origin privileges as far as the user can tell, and no way back except
 * restarting. `components/Markdown.tsx` therefore calls `preventDefault` on
 * every link click and routes it here instead.
 *
 * The command is the shell plugin's own `open`, not [`open_sign_in_url`]:
 * that one is pinned to `login.microsoftonline.com` on purpose and must stay
 * that way. `shell:allow-open` is already granted in
 * `src-tauri/capabilities/default.json`, and with no `plugins.shell.open`
 * override in `tauri.conf.json` the plugin applies its default scope —
 * `^((mailto:\w+)|(tel:\w+)|(https?://\w+)).+` — so the shell refuses anything
 * that is not an http(s), mailto or tel URL even if this file were wrong. That
 * is the second of two independent checks; the first is `safeHref` in
 * `lib/markdown.ts`, which is what decides a link may be rendered at all. No
 * capability or CSP change was needed for any of it.
 *
 * Outside the shell (`npm run dev` in a browser tab) it falls back to
 * `window.open` with `noopener`, which is the same "somewhere else, not here"
 * guarantee.
 */
export async function openExternal(url: string): Promise<void> {
  if (!url) return
  if (isTauri) {
    await invoke<void>("plugin:shell|open", { path: url })
    return
  }
  const opened = typeof window === "undefined" ? null : window.open(url, "_blank", "noopener,noreferrer")
  if (!opened) throw new Error("The browser blocked opening that link.")
}

/**
 * Subscribe to a shell event. Returns a cleanup function that is safe to call
 * even if the listener never attached.
 */
export function onShellEvent<T>(event: string, handler: (payload: T) => void): () => void {
  const listen = typeof window === "undefined" ? undefined : window.__TAURI__?.event?.listen
  if (!listen) return () => undefined

  let unlisten: UnlistenFn | null = null
  let cancelled = false

  void listen<T>(event, (e) => {
    if (!cancelled) handler(e.payload)
  })
    .then((fn) => {
      if (cancelled) fn()
      else unlisten = fn
    })
    .catch(() => undefined)

  return () => {
    cancelled = true
    unlisten?.()
    unlisten = null
  }
}

export interface DeepLinkTarget {
  kind: "approval" | "thread" | "bot" | "tab" | "auth" | "unknown"
  id: string
  /** Query parameters, for links that carry them (`auth`). */
  params: Record<string, string>
  raw: string
}

/**
 * `nesqbot://approval/<id>` · `nesqbot://thread/<id>` · `nesqbot://bot/<id>`
 * · `nesqbot://tab/<name>` · `nesqbot://auth?code=…&state=…`
 *
 * `auth` is recognised, and named, so that an OAuth redirect is never treated as
 * an unknown link and echoed back into the UI — the `code` in that URL is a
 * one-time credential and does not belong in a toast or a log line. `auth`
 * links are handed to `completeEntraRedirect` in `auth/entra.ts`; nothing else
 * may read `params` off one.
 */
export function parseDeepLink(raw: string): DeepLinkTarget {
  const cleaned = raw.trim()
  const withoutScheme = cleaned.replace(/^nesqbot:\/\//i, "").replace(/^\/+/, "")
  const [head = "", tail = ""] = withoutScheme.split(/[/?#]/, 2)
  const kind = head.toLowerCase()
  const id = decodeURIComponent(tail)

  const separator = withoutScheme.search(/[?#]/)
  const params: Record<string, string> = {}
  if (separator >= 0) {
    for (const [key, value] of new URLSearchParams(withoutScheme.slice(separator + 1))) {
      params[key] = value
    }
  }

  if (kind === "approval" || kind === "approvals") return { kind: "approval", id, params, raw: cleaned }
  if (kind === "thread" || kind === "threads") return { kind: "thread", id, params, raw: cleaned }
  if (kind === "bot" || kind === "bots") return { kind: "bot", id, params, raw: cleaned }
  if (kind === "tab") return { kind: "tab", id, params, raw: cleaned }
  if (kind === "auth") return { kind: "auth", id: "", params, raw: cleaned }
  return { kind: "unknown", id: withoutScheme, params, raw: cleaned }
}

/**
 * How Microsoft sign-in reaches the shell.
 *
 * Entra is provisioned for auth code + PKCE (docs/entra-setup.md) and the flow
 * needs three things a webview alone cannot do:
 *
 *  1. **Open the real browser.** `open_sign_in_url` in `src-tauri/src/lib.rs`
 *     does it, and refuses any URL that is not on
 *     `https://login.microsoftonline.com/`.
 *  2. **Receive the redirect.** That is `parseDeepLink` above, on the
 *     `deep-link` event.
 *  3. **Redeem the code.** This was the blocker. A webview `fetch` sends an
 *     `Origin` header, and Entra permits cross-origin redemption only for
 *     redirect URIs registered under the **Single-Page Application** platform.
 *     For the native public-client URIs this app uses it answers
 *     `AADSTS9002326` and sends no `Access-Control-Allow-Origin`, so the
 *     response would be unreadable even if it had succeeded. Widening the CSP
 *     does not help — the identity provider is refusing, not the browser.
 *
 *     `redeem_entra_code` / `refresh_entra_token` in `src-tauri/src/entra.rs`
 *     resolve it. They POST from a `reqwest` client owned by the shell, which
 *     sends no `Origin` header, so Entra sees the native public client it is.
 *     The authority and the endpoint path are pinned in Rust and only the
 *     tenant, client id, grant and scope cross IPC — the frontend cannot ask
 *     the shell to POST anywhere else, which is a narrower grant than an HTTP
 *     capability scoped by URL pattern would have been.
 *
 *     `tauri-plugin-http` was the previous attempt and does **not** work: its
 *     `fetch` runs in the Rust process but forwards the webview's origin, so
 *     Entra returned the identical `AADSTS9002326`, and setting `Origin: ""` —
 *     documented as suppressing the header — did not change it either. The
 *     plugin, its npm package and its capability entry have all been removed;
 *     nothing else in the app needed them.
 *
 * The two alternatives were rejected. Registering the loopback URI under the
 * Single-Page Application platform makes this an SPA client for that URI and
 * hands the token response to the webview; having the API redeem the code moves
 * a user credential through a server that does not otherwise need it. Both give
 * up the property that this is a native public client.
 *
 * The flow itself lives in `auth/entra.ts`; session state in `auth/index.tsx`.
 */
export const ENTRA_FLOW_SUMMARY = "Authorization code + PKCE; the code is redeemed in the Tauri shell, not the webview."
