import React from "react"
import ReactDOM from "react-dom/client"
import { nesqualMarkDataUri } from "@nesqbot/ui"
import { App } from "./App"
import { ErrorBoundary } from "./components/ErrorBoundary"
import { initMotion } from "./lib/motion"
import { applyTheme, readStoredTheme } from "./state/theme"
// Build-time design tokens (see the nesq-design-tokens plugin in vite.config.ts).
// Imported before `styles.css` so every custom property is defined by the time
// the rules that consume them are parsed.
import "virtual:nesq-tokens.css"
import "./styles.css"

/**
 * Boot.
 *
 * ## The rule this file now follows
 *
 * Nothing that runs before React mounts is allowed to take the application
 * down with it. Everything at module scope here touches the environment —
 * `localStorage`, `getComputedStyle`, `document.head` — and every one of those
 * is a thing that behaves differently in a packaged WebView than it does in
 * `vite dev`. A failure in any of them used to mean an empty `<body>`: no
 * React, so no error boundary, so no message, so nothing the user could report
 * beyond "it returns an empty screen".
 *
 * So each step is wrapped, each degrades to something the product can live
 * without, and if the whole thing still fails there is a painter of last
 * resort at the bottom that writes the reason into the page by hand.
 */

/** Run a boot step; never let it stop the boot. Returns whether it worked. */
function step(name: string, fn: () => void): boolean {
  try {
    fn()
    return true
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error(`[nesqbot] boot step "${name}" failed`, err)
    return false
  }
}

// Pick the scheme before the first paint so there is no flash of the wrong one.
// Reads `localStorage`, which throws outright when site data is blocked.
step("theme", () => applyTheme(readStoredTheme()))

/*
 * Register the design system's easing curves with GSAP and set the house
 * defaults. After `applyTheme`, because it reads the motion tokens off `:root`
 * and those only exist once the token stylesheet has been applied.
 *
 * Guarded, and this one is the point of the exercise: `initMotion` calls
 * `getComputedStyle(document.documentElement)` and registers `CustomEase`
 * curves parsed out of custom properties. A product that cannot animate is
 * inconvenient. A product that shows a white rectangle is broken. If this
 * throws, `dur()` falls back to its own hardcoded seconds and every component
 * animates on the defaults — nobody outside this file has to know.
 */
step("motion", initMotion)

/**
 * The window icon, from the same vector the sidebar draws. Tauri sets the
 * taskbar icon from `bundle.icon`, but the WebView still asks for a favicon and
 * a 404 for /favicon.ico is both a console error and, in dev, a broken tab.
 * Percent-encoded SVG, so it costs a few hundred bytes and no request.
 */
function installFavicon(): void {
  const href = nesqualMarkDataUri({ size: 64 })
  let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
  if (!link) {
    link = document.createElement("link")
    link.rel = "icon"
    document.head.appendChild(link)
  }
  link.type = "image/svg+xml"
  link.href = href
}

step("favicon", installFavicon)

/* ------------------------------------------------------------------ *
 * The painter of last resort
 * ------------------------------------------------------------------ */

/**
 * Has React actually put something on screen?
 *
 * Asked of the DOM rather than tracked with a flag set next to `render()`.
 * `root.render()` only *schedules* work, so a flag flipped beside it claims a
 * mount that may never have committed — which is precisely the case the
 * painter below exists for. An element inside `#root` is the only honest
 * evidence that the tree is up.
 */
function hasRendered(): boolean {
  return (document.getElementById("root")?.firstElementChild ?? null) !== null
}

/**
 * Write a readable failure into `#root` using DOM calls only.
 *
 * Three constraints shape this:
 *
 *  - **No React.** By the time this runs, React is the thing that failed.
 *  - **No inline styles.** Tauri rewrites the CSP and substitutes a nonce into
 *    `style-src`; under CSP Level 3 a nonce makes `'unsafe-inline'` be ignored,
 *    so a `style="…"` attribute is refused. Every rule here comes from
 *    `styles.css`, which is a `'self'` stylesheet and always allowed. If even
 *    that failed to load the markup is still plain readable text.
 *  - **No `innerHTML`.** The message can contain anything, including the text
 *    of a page the bot was reading.
 */
function paintFatal(title: string, detail: string): void {
  const container = document.getElementById("root")
  if (!container || hasRendered()) return

  container.textContent = ""

  const wrap = document.createElement("div")
  wrap.className = "crash"
  wrap.setAttribute("role", "alert")

  const card = document.createElement("div")
  card.className = "crash__card"

  const eyebrow = document.createElement("div")
  eyebrow.className = "eyebrow"
  eyebrow.textContent = "Nesq Bot"

  const heading = document.createElement("h1")
  heading.className = "crash__title"
  heading.textContent = title

  const body = document.createElement("p")
  body.className = "crash__body"
  body.textContent =
    "The interface could not start. Your bots are unaffected — they run in Azure and keep going whether this " +
    "window is open or not."

  const pre = document.createElement("pre")
  pre.className = "crash__detail"
  pre.textContent = detail

  const actions = document.createElement("div")
  actions.className = "crash__actions"

  const reload = document.createElement("button")
  reload.type = "button"
  reload.className = "btn btn--primary"
  reload.textContent = "Reload the app"
  reload.addEventListener("click", () => location.reload())

  const copy = document.createElement("button")
  copy.type = "button"
  copy.className = "btn btn--ghost"
  copy.textContent = "Copy error details"
  copy.addEventListener("click", () => {
    void navigator.clipboard
      .writeText(`Nesq Bot failed to start\n${new Date().toISOString()}\n${navigator.userAgent}\n\n${detail}`)
      .then(() => {
        copy.textContent = "Copied"
      })
      .catch(() => {
        copy.textContent = "Copy failed"
      })
  })

  actions.append(reload, copy)
  card.append(eyebrow, heading, body, pre, actions)
  wrap.append(card)
  container.append(wrap)
}

function describe(value: unknown): string {
  if (value instanceof Error) return `${value.name}: ${value.message}\n\n${value.stack ?? "(no stack)"}`
  if (typeof value === "string") return value
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}

/**
 * Paint, but only after giving React the chance to do it better.
 *
 * React reports a render error to `window` (via `reportError`) *before* it has
 * committed the boundary's fallback, so a handler that paints immediately wins
 * a race it should lose: the boundary's screen carries the component stack and
 * a working reset, and this one does not. Deferring a frame and re-checking
 * means the boundary is used whenever there is a boundary, and this is what is
 * left when there is not.
 */
function paintFatalSoon(title: string, detail: string): void {
  setTimeout(() => {
    if (hasRendered()) return
    paintFatal(title, detail)
  }, 60)
}

/*
 * A throw that escapes React entirely, or a rejected promise nobody handled.
 * These only paint while the tree is still empty: once React is up, its own
 * root boundary owns the screen and a late async error must not blow the
 * user's workspace away.
 */
window.addEventListener("error", (event) => {
  if (hasRendered()) return
  paintFatalSoon("Nesq Bot could not start", describe(event.error ?? event.message))
})

window.addEventListener("unhandledrejection", (event) => {
  if (hasRendered()) return
  paintFatalSoon("Nesq Bot could not start", describe(event.reason))
})

/* ------------------------------------------------------------------ *
 * Mount
 * ------------------------------------------------------------------ */

const container = document.getElementById("root")

if (!container) {
  // Nothing to paint into; the console is the only channel left.
  // eslint-disable-next-line no-console
  console.error("[nesqbot] Root container missing from index.html")
} else {
  try {
    ReactDOM.createRoot(container).render(
      <React.StrictMode>
        {/*
          The root boundary. Everything below it — the shell, the sidebar, the
          toast viewport, the takeover beacon, and `Shell`'s own `useGSAP` —
          used to be able to unmount the whole tree by throwing. The per-panel
          boundaries in `App.tsx` stay: they are finer-grained and keep one bad
          panel from taking the workspace with it. This one is the floor.
        */}
        <ErrorBoundary label="Nesq Bot" variant="root">
          <App />
        </ErrorBoundary>
      </React.StrictMode>,
    )
  } catch (err) {
    paintFatal("Nesq Bot could not start", describe(err))
  }

  /*
   * The watchdog.
   *
   * `render()` schedules; it does not draw. If the commit never happens — and
   * it never happened for the people looking at an empty window — no exception
   * necessarily reaches `catch` or the `error` listener, and the page simply
   * stays blank forever. Three seconds later, if `#root` is still empty, say
   * so. Wrong only in the case where the app is merely very slow to first
   * paint, and a first paint that takes over three seconds is worth a complaint
   * of its own.
   */
  setTimeout(() => {
    if (hasRendered()) return
    paintFatal(
      "Nesq Bot did not finish starting",
      "The interface was asked to render and nothing appeared within three seconds, with no error reported.\n\n" +
        "This is usually a build or content-security-policy problem rather than a fault with your account. " +
        "Reloading is worth one try; if it happens again, send these details to support.",
    )
  }, 3000)
}
