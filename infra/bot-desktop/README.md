# Bot Desktop

A disposable Linux desktop that one bot drives. Debian + a window manager +
x11vnc/noVNC for the stream + a FastAPI sidecar for computer use.

| Port | What                                                                                                                    |
| ---- | ------------------------------------------------------------------------------------------------------------------------- |
| 6901 | noVNC stream, embeddable in the Tauri pane and the Expo WebView                                                           |
| 7910 | agent sidecar: pixel API (`/health`, `/screenshot`, `/windows`, `/action`, `/clipboard_get`) and CDP API (`/browser/*`)   |
| 9222 | Chromium DevTools - **loopback only, never published**, see [Browser control](#browser-control-browser)                   |

`/home/nesq` is the persisted bot home - a bind mount locally, a PVC on AKS.

## Build

```bash
# slim (default): IceWM only, ~40% smaller
docker build -t nesqbot/bot-desktop:local infra/bot-desktop

# full: also installs XFCE, for bots whose desktop_profile is xfce
docker build --build-arg DESKTOP_PROFILE_BUILD=full \
  -t nesqbot/bot-desktop:xfce infra/bot-desktop

# or through compose
docker compose --profile desktop build bot-desktop
```

`DESKTOP_PROFILE=xfce` on a slim image logs a warning and falls back to IceWM
rather than crash-looping.

Reproducible builds pin the base by digest instead of pinning apt packages
(which rot within weeks on a Debian point release):

```bash
docker build --build-arg DEBIAN_TAG=bookworm-slim@sha256:... infra/bot-desktop
```

## Run one by hand

```bash
docker run --rm -it \
  -p 6901:6901 -p 7910:7910 \
  --shm-size 512m \
  -e BOT_SLUG=sales \
  -e DESKTOP_RESOLUTION=1920x1080x24 \
  -e VNC_PW=nesq \
  -e NESQ_SIDECAR_TOKEN="$(openssl rand -base64 32)" \
  -v "$PWD/data/bot-homes/sales:/home/nesq" \
  nesqbot/bot-desktop:local
```

Stream: <http://localhost:6901/vnc.html>. The container reports `READY` only
once the sidecar answers `/health`, so watching for that line is a reliable
readiness signal.

## Runtime environment

| Var                     | Default       | Notes                                           |
| ----------------------- | ------------- | ----------------------------------------------- |
| `DESKTOP_PROFILE`       | `icewm`       | `icewm` or `xfce` (needs a `full` image)        |
| `DESKTOP_RESOLUTION`    | `1440x900x24` | or `DESKTOP_WIDTH`/`HEIGHT`/`DEPTH`             |
| `VNC_PW`                | _empty_       | empty = unauthenticated stream + warning        |
| `NESQ_SIDECAR_TOKEN`    | _empty_       | empty = unauthenticated control plane + warning |
| `NESQ_SIDECAR_PORT`     | `7910`        |                                                 |
| `NESQ_STREAM_PORT`      | `6901`        |                                                 |
| `READY_TIMEOUT_SECONDS` | `90`          | how long to wait for the sidecar before warning |
| `LOG_LEVEL`             | `INFO`        |                                                 |

Browser control (see [Browser control](#browser-control-browser)):

| Var                          | Default       | Notes                                            |
| ---------------------------- | ------------- | ------------------------------------------------ |
| `NESQ_BROWSER_ENABLED`       | `1`           | `0` skips launching Chromium entirely            |
| `NESQ_CDP_PORT`              | `9222`        | always bound to 127.0.0.1                        |
| `NESQ_BROWSER_START_URL`     | `about:blank` | first tab                                        |
| `NESQ_BROWSER_PROFILE_DIR`   | `~/.config/chromium-nesq` | shared with the `open_chromium` action |
| `NESQ_CHROMIUM_SANDBOX`      | `auto`        | `auto` probes user namespaces, else `on` / `off` |
| `NESQ_CHROMIUM_EXTRA_ARGS`   | _empty_       | extra Chromium flags, word-split                 |
| `NESQ_CDP_TIMEOUT`           | `20`          | seconds, ceiling on one CDP round trip           |
| `NESQ_SNAPSHOT_MAX_ELEMENTS` | `200`         | default snapshot element cap                     |
| `NESQ_SNAPSHOT_MAX_TEXT`     | `40`          | default static-text line cap                     |
| `NESQ_SNAPSHOT_MAX_BYTES`    | `24000`       | hard byte cap on a rendered snapshot             |

## Supervision

`entrypoint.sh` runs each component (Xvfb, WM, x11vnc, websockify, sidecar) in
its own restart loop with exponential backoff. The previous `wait -n` version
exited as soon as _any_ component died, so a crashed x11vnc silently killed the
container. SIGTERM tears everything down in reverse dependency order with a 10s
grace period before SIGKILL.

## Sidecar auth

Every endpoint except `/health` requires `X-Nesq-Sidecar-Token`:

```bash
curl -H "X-Nesq-Sidecar-Token: $NESQ_SIDECAR_TOKEN" \
  'http://localhost:7910/screenshot?format=jpeg&quality=70&max_width=1024'
```

`/health` is deliberately open so kubelet probes and the Docker `HEALTHCHECK`
work without the secret; it reports capability (display reachable, tools
present, screen size) and never content.

With the token unset the sidecar keeps working for local dev and logs a loud
warning on boot and on unauthenticated calls.

### Payload size

A full-screen PNG is roughly 1.5 MB of base64 per step. Use region cropping and
JPEG to keep model context affordable:

```
GET /screenshot?x=0&y=0&w=800&h=600&format=jpeg&quality=70
GET /screenshot?max_width=1024&format=jpeg          # whole screen, downscaled
```

PNG responses keep the `png_base64` key pinned by `docs/API.md`; JPEG responses
return `image_base64` + `mime`.

### Actions

`POST /action` with `{"action": ..., ...}`:

`click`, `double_click`, `right_click`, `middle_click`, `mousedown`, `mouseup`,
`mousemove`, `drag` (`x,y -> to_x,to_y`, interpolated), `scroll`
(`direction`, `amount`), `type`, `key`, `key_combo` (`ctrl+shift+t`),
`clipboard_set`, `open_chromium`, `focus_window`, `close_window`.

All of them shell out with a fixed argv - never a shell string - and every
subprocess has a hard timeout, so a modal dialog cannot wedge a request thread.

This surface is frozen: the desktop app, the mobile app and the orchestrator
all send it. For web pages prefer `/browser/*` below - same browser, ~40x fewer
bytes and no coordinate guessing - and keep this as the fallback.

## Browser control (`/browser/*`)

The pixel API makes a model look at a 1280x800 screenshot and guess
coordinates. For a web page that is the wrong interface. `/browser/*` drives
the same Chromium over the Chrome DevTools Protocol, so a page arrives as a few
KB of `ref role "name"` lines and actions address elements by reference.

Measured on this image, same page, same moment:

| Page                     | `/browser/snapshot` | `/screenshot` PNG response | JPEG q70 @1024 |
| ------------------------ | ------------------- | -------------------------- | -------------- |
| local test bench         | 879 B               | 213 562 B (**243x**)       | 51 490 B (59x) |
| github.com               | 7 722 B             | 409 611 B (**53x**)        | 53 611 B (7x)  |
| en.wikipedia.org article | 12 672 B            | 540 811 B (**42x**)        | 89 626 B (7x)  |
| news.ycombinator.com     | 11 571 B            | 422 955 B (**37x**)        | 68 633 B (6x)  |

The screenshot column is the full JSON response, which is what actually crosses
the wire (base64 inflates the image by 4/3). Against an aggressively downscaled
JPEG the byte win narrows to ~6x, and in _tokens_ a full 200-element snapshot
(~3 000 text tokens) can even exceed a 1024-wide screenshot (~1 300 vision
tokens). The snapshot's advantage there is not size, it is that every line
carries an actionable `ref` and the model never has to estimate a pixel. Use
`viewport_only`, `max_elements` and `name_filter` when context is tight -
Wikipedia drops from 12 672 B to 4 169 B with `viewport_only`, and to 2 965 B
at `max_elements=60`.

The pixel API is unchanged and remains the fallback for canvas, CAPTCHAs, PDF
viewers, `<video>` and every non-browser app on the desktop.

### Where Chromium lives

`entrypoint.sh` supervises Chromium with
`--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1` on the same
X display, so a bot can still _see_ it over VNC and still click it with the
pixel API. The profile is `~/.config/chromium-nesq`, the same one the
`open_chromium` action uses, so a tab opened that way appears in
`/browser/tabs`.

The debugging port is unauthenticated. Anything that can open a socket to it
owns the browser and every session logged into it. Therefore:

- it binds to `127.0.0.1` (verify: `grep -i :2406 /proc/net/tcp` shows
  `0100007F:2406 ... 0A`, versus `00000000:1EE6` for the sidecar on 7910);
- it is not in `EXPOSE`, and publishing it with `-p` still gets you nothing - a
  peer container gets ECONNREFUSED and a host `curl` to the published port gets
  an empty reply;
- `--remote-allow-origins` is deliberately **not** set, so a page the browser
  loads cannot open a WebSocket to its own debugger. The sidecar's client sends
  no `Origin` header instead.

The sidecar is the only client, and it sits behind `X-Nesq-Sidecar-Token` like
every other non-`/health` route.

Chromium's own renderer sandbox is kept when the kernel allows it: the
entrypoint probes `unshare --user` and only adds `--no-sandbox` when
unprivileged user namespaces are unavailable (which is the case under Docker
Desktop and under `allowPrivilegeEscalation: false`, where the setuid helper
cannot elevate either). Override with `NESQ_CHROMIUM_SANDBOX=on|off`.

### There is no `/browser/eval`

Deliberately. A "run this JavaScript" endpoint would make everything below
redundant and would turn any leak of the sidecar token - or any SSRF that can
reach 7910 - into arbitrary code execution inside the bot's logged-in sessions.
Every `Runtime.evaluate` / `Runtime.callFunctionOn` in `sidecar/browser.py` uses
a fixed function literal defined in that file; caller input only ever arrives as
_arguments_ (CSS selectors, strings, numbers). A CSS selector can read; it
cannot call, assign or navigate. If a page needs something the endpoints below
cannot express, add a named fixed function - do not open the general door.

### Error contract

Success is always `200` with `{"ok": true, ...}`. Failures are non-2xx with
`{"ok": false, "error": "<code>", "detail": "...", ...}`:

| HTTP | `error`                                                                                                                      | Meaning                                                    |
| ---- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| 400  | `url_not_allowed`, `bad_selector`, `unknown_key`, `missing_selector`                                                         | bad request                                                |
| 409  | `unknown_ref`, `stale_ref`, `not_actionable`, `obscured`, `select_failed`, `unknown_target`, `no_dialog`, `no_history_entry` | the reference or the page state refuses the action         |
| 422  | (FastAPI validation)                                                                                                         | malformed body                                             |
| 502  | `cdp_error`, `navigation_failed`                                                                                             | Chromium said no                                           |
| 503  | `browser_unavailable`                                                                                                        | Chromium is not answering; fall back to the pixel API      |
| 504  | `cdp_timeout`, `wait_timeout`                                                                                                | timed out; `pending_dialog` is set if a modal is the cause |

`409` is the interesting one: it means _"I refused rather than clicked the wrong
thing"_, and the correct response is almost always to re-snapshot.

### References and how they stay honest

`/browser/snapshot` mints refs (`e17`) belonging to a snapshot (`s3`). A ref
stores the element's CDP `backendNodeId`, its accessible role and name, its tag
and a fallback attribute key, plus the tab and the main frame's `loaderId`.
Before any action the sidecar re-verifies all of it:

1. the ref exists in one of the last 4 snapshots, else `unknown_ref`;
2. if the caller passed `snapshot_id`, it must match, else `stale_ref`;
3. the tab still exists (and is brought to the foreground if it was in the
   background - Chromium throttles background renderers, which turned a 160 ms
   click into a 5 s one; the response then carries `switched_to_tab`);
4. the main frame's `loaderId` is unchanged - **any navigation invalidates every
   ref on that tab**, `stale_ref`;
5. the node is still attached, else `stale_ref`;
6. Chrome's accessibility tree is re-queried for that exact `backendNodeId` and
   its role and name must still match, else `stale_ref` with `expected` and
   `actual` in the body;
7. it has a rendered box, is visible, is enabled and lands inside the viewport,
   else `not_actionable`;
8. `document.elementFromPoint` at the click point must resolve to the element or
   a relative of it, else `obscured` - naming what is on top.

Every check except 1 and 2 can be bypassed with `"force": true`, which logs a
warning. Nothing bypasses them silently.

Observed: clicking a button that renames itself, then reusing the ref, returns

```json
{
  "ok": false,
  "error": "stale_ref",
  "detail": "e10 now resolves to a different element",
  "expected": { "role": "button", "name": "Rename me" },
  "actual": { "role": "button", "name": "I was renamed" },
  "hint": "re-snapshot; pass force=true only if you are sure"
}
```

### Snapshot format

`POST /browser/snapshot` with `format: "lines"` (the default) renders one
element per line, indented by accessibility depth:

```
e1 heading "Nesq CDP Test Bench"
text "A form, a list, a link and things that change when you click."
e2 textbox "Email address"
e3 combobox "Plan" value="Free"
  e4 option "Free" [selected]
  e5 option "Pro"
e7 checkbox "I accept the terms"
e9 button "Create account"
e11 button "Cannot press" [disabled]
e13 link "Go to the detail page" -> /detail.html
--- iframe https://example.com/ ---
e15 link "Learn more" -> https://iana.org/domains/example
... 493 more interactive elements not shown (raise max_elements, or narrow with name_filter / role_filter)
```

Grammar: `<ref> <role> "<accessible name>" [value="..."] [-> href] [flags]`.
Flags come from the accessibility tree:
`disabled checked mixed expanded required focused selected invalid pressed readonly`.
Static text lines have no ref. Cross-origin iframes are attached as separate CDP
sessions and appended under a `--- iframe <url> ---` marker; their elements are
clickable, because `DOM.getContentQuads` returns top-frame coordinates.

Names come from Chrome's own accessibility name computation, not from markup
scraping. Text that only repeats an adjacent control's name is dropped, and the
subtree of any control that was filtered out or truncated away is dropped with
it - otherwise a 500-button page returns 400 lines of label text for elements
you cannot reference.

Truncation is never silent: the response carries `interactive_total`, `matched`
(after filters), `returned`, `truncated`, and the rendered text ends with a line
saying how many were dropped. A separate `NESQ_SNAPSHOT_MAX_BYTES` (24 KB) cap
cuts at a line boundary and sets `byte_capped: true`.

`format: "json"` returns the same rows as
`elements: [{kind, depth, ref, role, name, value, tag, flags, href}]`, roughly
3x the bytes of `lines`; prefer `lines` for anything going into a model.

### Endpoints

All are `POST` with a JSON body unless marked, all require
`X-Nesq-Sidecar-Token`, and all take an optional `target_id` to address a tab
other than the active one.

| Endpoint                 | Request                                                                                                                                                | Response                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `GET /browser/status`    | -                                                                                                                                                      | `{ok, browser, protocol, endpoint, active_target, tabs[], snapshots_live[], pending_dialogs[]}` |
| `/browser/navigate`      | `{url, target_id?, wait_until?: load\|domcontentloaded\|networkidle\|none, timeout_ms?=30000}`                                                         | `{ok, url, title, loader_id, target_id, load_state, took_ms}`                                   |
| `/browser/snapshot`      | `{target_id?, max_elements?=200, include_text?=true, max_text_nodes?=40, name_filter?, role_filter?, viewport_only?=false, format?=lines\|json}`       | `{ok, snapshot_id, target_id, url, title, interactive_total, matched, returned, truncated, frames, snapshot, bytes}` |
| `/browser/click`         | `{ref, snapshot_id?, button?=left\|right\|middle, click_count?=1, modifiers?=0, force?=false}`                                                         | `{ok, ref, role, name, target_id, took_ms, x, y, new_tabs[], switched_to_tab?, pending_dialog?}` |
| `/browser/type`          | `{ref, text, clear?=true, submit?=false, mode?=auto\|keys\|insert, delay_ms?=0, snapshot_id?, force?}`                                                 | `{ok, ref, ..., chars, mode, submitted?}`                                                       |
| `/browser/select`        | `{ref, values: [...]}` - matches option value, label or text                                                                                           | `{ok, ref, selected: [{value, label}]}`; `409 select_failed` lists the real options             |
| `/browser/hover`         | `{ref, snapshot_id?, force?}`                                                                                                                          | `{ok, ref, x, y}`                                                                               |
| `/browser/scroll`        | `{ref}` to scroll an element into view, or `{direction, amount_px?=600, target_id?}`                                                                   | `{ok, x, y, maxY}` or `{ok, ref, scrolled_into_view}`                                           |
| `/browser/key`           | `{key, modifiers?=0, target_id?}` - `Enter Tab Escape Backspace Delete Arrow* Home End PageUp PageDown Space`; modifiers `1=alt 2=ctrl 4=meta 8=shift` | `{ok, key, modifiers}`                                                                          |
| `/browser/text`          | `{selector?, max_chars?=20000, target_id?}`                                                                                                            | `{ok, url, title, text, length, truncated}`                                                     |
| `/browser/extract`       | `{selector, fields?: [{name, selector?, attr?}], limit?=100, target_id?}`                                                                              | `{ok, total, returned, truncated, rows: [{...}]}`                                               |
| `/browser/wait`          | `{until: load\|selector\|text\|gone, selector?, text?, state?=visible\|attached\|hidden\|detached, timeout_ms?=10000}`                                 | `{ok, until, state, waited_ms}` or `504 wait_timeout`                                           |
| `GET /browser/tabs`      | -                                                                                                                                                      | `{ok, count, active_target, tabs: [{target_id, url, title, active}]}`                           |
| `/browser/tabs/new`      | `{url?=about:blank, activate?=true}`                                                                                                                   | `{ok, target_id, url}`                                                                          |
| `/browser/tabs/activate` | `{target_id}`                                                                                                                                          | `{ok, active_target, url, title, loader_id}`                                                    |
| `/browser/tabs/close`    | `{target_id}`                                                                                                                                          | `{ok, closed}`                                                                                  |
| `/browser/back`          | `{target_id?}`                                                                                                                                         | `{ok, url, title, loader_id}`                                                                   |
| `/browser/forward`       | `{target_id?}`                                                                                                                                         | `{ok, url, title, loader_id}`                                                                   |
| `/browser/reload`        | `{target_id?, ignore_cache?=false}`                                                                                                                    | `{ok, url, title, loader_id, load_state}`                                                       |
| `/browser/dialog`        | `{accept, prompt_text?, target_id?}`                                                                                                                   | `{ok, handled: {type, message, url}}`                                                           |

`navigate` and `tabs/new` accept only `http(s)://`, `about:blank` and
`file:///home/nesq/`. `javascript:` and `data:` URLs are rejected with
`400 url_not_allowed` - a `javascript:` navigation is an eval endpoint wearing a
hat.

`type` defaults to `mode=auto`: real per-character key events up to 200
characters (so search-as-you-type and autocomplete fire), `Input.insertText`
beyond that for speed. Force either with `mode`.

`/health` gains a non-gating `browser` block:

```json
"browser": {"cdp_enabled": true, "cdp_endpoint": "127.0.0.1:9222",
            "cdp_reachable": true, "loopback_only": true,
            "browser": "Chrome/151.0.7922.137", "tabs": 1}
```

It deliberately does **not** affect `ok`. A desktop with a wedged Chromium is
still drivable by pixels, and the readiness probe should not restart it. If
`browser.py` fails to import at all, the sidecar logs an error, serves the pixel
API normally, and reports the import error here.

### Worked example

```bash
H='X-Nesq-Sidecar-Token: '"$NESQ_SIDECAR_TOKEN"; B=http://localhost:7910
post(){ curl -s -H "$H" -H 'Content-Type: application/json' -X POST "$B$1" -d "$2"; }

post /browser/navigate '{"url":"https://example.com/signup"}'
post /browser/snapshot '{}'                       # -> e2 textbox "Email address", ...
post /browser/type     '{"ref":"e2","text":"a@b.com"}'
post /browser/select   '{"ref":"e3","values":["Pro"]}'
post /browser/click    '{"ref":"e7"}'
post /browser/click    '{"ref":"e9"}'
post /browser/text     '{"selector":"#result"}'
```

### What a real site does that a test page does not

Found while driving github.com, wikipedia.org and news.ycombinator.com from this
image:

- **Cookie/consent banners** cover the controls underneath. That is what the
  `obscured` error is for - it names the covering element so the agent can
  dismiss it first instead of clicking a banner three times.
- **Off-canvas elements.** Wikipedia's "Jump to content" skip link is the first
  focusable thing on the page and lives at `(-0.5, -0.5)`. It has a box and a
  positive size, so it passes every naive visibility check; only the explicit
  viewport-bounds test rejects it.
- **Element counts in the hundreds.** A Wikipedia article has 442 interactive
  elements. The default 200 cap plus `viewport_only` / `name_filter` is what
  keeps a snapshot usable; the API lane should expose those knobs as tool
  parameters rather than hide them.
- **Cross-origin iframes** (consent frames, payment fields, embedded widgets)
  are separate processes. They are attached and snapshotted, but an iframe that
  appears _after_ the first snapshot is only attached by then - snapshot twice
  if a widget loads late.
- **Blocking `alert()`/`confirm()`** freeze the renderer, so every CDP call then
  times out at 10-20 s. The sidecar reports `pending_dialog` on those errors, a
  click that opened one still returns `ok: true` with `pending_dialog` (it did
  land - retrying would double-fire it), and `/browser/dialog` clears it.
- **ARIA comboboxes** (a `div` listbox, not a `<select>`) do not work with
  `/browser/select`; it returns `409 select_failed` with
  `reason: not_a_select`. Click the control, re-snapshot, click the option.
- **Shadow DOM** is pierced by the accessibility tree, so web components work.
- **`<canvas>` apps, CAPTCHAs, PDF.js and `<video>`** expose nothing useful in
  the accessibility tree. That is the hybrid boundary: fall back to
  `/screenshot` + `/action`.
- **Background tabs** are throttled by Chromium. Acting on a ref in one
  auto-foregrounds it first; the response says `switched_to_tab`.

### Dependency

One new pin, `websocket-client==1.8.0` - a pure-Python wheel with no compiled
extensions. The HTTP half of CDP (`/json/version`, `/json/list`) goes through
stdlib `urllib`, so that is the entire transport. Playwright and pyppeteer were
rejected: both want to manage their own browser binary, and this image already
carries Chromium and is 1.66 GB.

Image cost: **+130 844 bytes compressed** (422 746 540 -> 422 877 384, measured
against an otherwise identical image built without the dependency and without
`browser.py`), and about 310 KB unpacked (196 KB `websocket/`, 28 KB dist-info,
85 KB `browser.py`). Both images still report `1.66GB`.

## Kubernetes

`k8s/desktop-template.yaml` is a placeholder template, not an applyable
manifest. It carries the Deployment, Service, PVC, ServiceAccount,
NetworkPolicy and PDB. Substitute `BOT_SLUG`, `ACR_LOGIN_SERVER`, `IMAGE_TAG`,
`NAMESPACE` and `STORAGE_CLASS`, then pipe to `kubectl apply -f -`. The header
of that file has the exact `sed` line and the Secret it expects.

## Push to ACR

```bash
az acr login -n "$ACR_NAME"
docker tag nesqbot/bot-desktop:local "$ACR_NAME.azurecr.io/nesqbot/bot-desktop:v0.1.0"
docker push "$ACR_NAME.azurecr.io/nesqbot/bot-desktop:v0.1.0"
```

Tag with a version. `:latest` on a desktop image makes "which build was this
bot running when it clicked that" unanswerable.
