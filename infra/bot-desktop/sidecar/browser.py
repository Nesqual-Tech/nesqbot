"""CDP browser control for the Bot Desktop sidecar.

Why this exists
---------------
The pixel API (``/screenshot`` + ``/action``) makes a model look at a 1280x800
PNG and guess coordinates. For web pages that is the wrong interface: it costs
~500 KB of image per step, vision tokens cost several times text tokens, and
the model still misses. This module drives the same Chromium through the
Chrome DevTools Protocol instead, so a page arrives as a few KB of
``ref role "name"`` lines and actions address elements by reference.

The pixel API is untouched and stays the fallback for canvas, CAPTCHAs, PDF
viewers and every non-browser app on the desktop.

Security model
--------------
* Chromium's debugging port is unauthenticated and equivalent to full control
  of the browser (and of every logged-in session in its profile). The
  entrypoint therefore binds it to 127.0.0.1 and it is never EXPOSEd. The
  sidecar - which does have a shared-secret gate - is the only client.
* There is deliberately **no endpoint that evaluates caller-supplied
  JavaScript**. Every ``Runtime.evaluate`` / ``Runtime.callFunctionOn`` in this
  file uses a fixed function literal defined here; caller input only ever
  arrives as *arguments* to those functions (CSS selectors, strings, numbers),
  never as code. See the module docstring section "Why no /browser/eval" at the
  bottom of this file.

Transport
---------
One websocket to the browser-level CDP endpoint, with flat sessions
(``Target.attachToTarget {flatten: true}``) multiplexed over it. A single
reader thread demultiplexes replies by id and fans events out to waiters, so
the FastAPI threadpool endpoints can stay synchronous like the rest of the
sidecar.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:  # pragma: no cover - the image always has it; a bare checkout may not.
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore[assignment]

logger = logging.getLogger("nesq.sidecar.browser")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CDP_HOST = os.getenv("NESQ_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.getenv("NESQ_CDP_PORT", "9222"))
# Ceiling on any single CDP round trip. A page running a blocking alert() or a
# synchronous XHR will hit this instead of wedging a worker thread forever.
CDP_TIMEOUT = float(os.getenv("NESQ_CDP_TIMEOUT", "20"))
NAV_TIMEOUT = float(os.getenv("NESQ_CDP_NAV_TIMEOUT", "30"))

# Snapshot ceilings. These are what keep a snapshot a few KB instead of a DOM
# dump; see /browser/snapshot for how truncation is reported.
SNAPSHOT_MAX_ELEMENTS = int(os.getenv("NESQ_SNAPSHOT_MAX_ELEMENTS", "200"))

#: Wall-clock budget for one whole snapshot, across every frame.
#:
#: `CDP_TIMEOUT` bounds a single round trip; this bounds the operation. Without
#: it a page with many iframes multiplies the per-call timeout by the frame count
#: and one `browser_snapshot` on LinkedIn measured 1m05s against 168ms on a light
#: page. Eight seconds is comfortably more than a heavy page needs when it is
#: behaving, and far less than a person will wait for a bot to read a screen.
SNAPSHOT_BUDGET_SECONDS = float(os.getenv("NESQ_SNAPSHOT_BUDGET_SECONDS", "8"))
SNAPSHOT_MAX_TEXT_NODES = int(os.getenv("NESQ_SNAPSHOT_MAX_TEXT", "40"))
SNAPSHOT_NAME_CHARS = 120
SNAPSHOT_TEXT_CHARS = 160
SNAPSHOT_MAX_BYTES = int(os.getenv("NESQ_SNAPSHOT_MAX_BYTES", "24000"))
SNAPSHOT_KEEP = 4  # how many past snapshots stay resolvable

ALLOWED_URL_RE = re.compile(r"^(https?://|about:blank$|file:///home/nesq/)")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BrowserError(Exception):
    """Every failure this module reports.

    ``code`` is the machine-readable contract; ``status`` decides the HTTP
    code so a caller can branch on the class of failure without parsing text.
    """

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        status: int = 400,
        **extra: Any,
    ) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.status = status
        self.extra = extra

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": self.code}
        if self.detail:
            out["detail"] = self.detail
        out.update(self.extra)
        return out


async def browser_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BrowserError)
    if exc.status >= 500:
        logger.warning("browser error %s: %s", exc.code, exc.detail)
    return JSONResponse(exc.body(), status_code=exc.status)


def _unavailable(detail: str) -> BrowserError:
    return BrowserError(
        "browser_unavailable",
        detail,
        status=503,
        hint=(
            f"Chromium is not answering CDP on {CDP_HOST}:{CDP_PORT}. "
            "The entrypoint supervises it; check container logs. "
            "The pixel API (/screenshot, /action) still works."
        ),
    )


# --------------------------------------------------------------------------- #
# CDP transport
# --------------------------------------------------------------------------- #
@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class _Waiter:
    session_id: str | None
    method: str
    predicate: Callable[[dict[str, Any]], bool]
    event: threading.Event = field(default_factory=threading.Event)
    payload: dict[str, Any] | None = None


class CDPConnection:
    """One websocket to the browser target, with flat sessions on top."""

    def __init__(self, host: str = CDP_HOST, port: int = CDP_PORT) -> None:
        self.host = host
        self.port = port
        self._ws: Any = None
        # Reentrant on purpose: connect() and send() both take it, and the
        # connect path legitimately needs to send.
        self._lock = threading.RLock()         # guards socket writes + id alloc
        self._state_lock = threading.RLock()   # guards dicts below
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._waiters: list[_Waiter] = []
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()
        self._sessions: dict[str, str] = {}    # target_id -> session_id
        self._session_targets: dict[str, str] = {}  # session_id -> target_id
        self._iframe_sessions: dict[str, set[str]] = {}  # page sess -> child sess
        self._domains_ready: set[str] = set()
        self.dialogs: dict[str, dict[str, Any]] = {}     # session_id -> dialog
        self.browser_version: dict[str, Any] = {}

    # -- HTTP side of CDP (target discovery) --------------------------------
    def http_json(self, path: str, timeout: float = 5.0) -> Any:
        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:
            raise _unavailable(f"GET {path} failed: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise _unavailable(f"GET {path} failed: {exc}") from exc

    def reachable(self) -> bool:
        try:
            self.browser_version = self.http_json("/json/version", timeout=2.0)
            return True
        except BrowserError:
            return False

    # -- connection ----------------------------------------------------------
    def _connect_locked(self) -> None:
        if websocket is None:
            raise _unavailable("the websocket-client package is not installed")
        version = self.http_json("/json/version")
        self.browser_version = version
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise _unavailable("/json/version has no webSocketDebuggerUrl")
        # No Origin header is sent, which is what keeps Chrome >=111 from
        # rejecting us without --remote-allow-origins (a flag that would widen
        # who can drive this browser, so we deliberately do not set it).
        conn = websocket.create_connection(
            ws_url,
            timeout=10,
            enable_multithread=True,
            skip_utf8_validation=True,
            suppress_origin=True,
        )
        conn.settimeout(None)
        self._ws = conn
        self._closed = threading.Event()
        self._pending.clear()
        self._waiters.clear()
        self._sessions.clear()
        self._session_targets.clear()
        self._iframe_sessions.clear()
        self._domains_ready.clear()
        self.dialogs.clear()
        self._reader = threading.Thread(
            target=self._read_loop, args=(conn, self._closed), daemon=True,
            name="cdp-reader",
        )
        self._reader.start()
        logger.info("cdp connected endpoint=%s", version.get("Browser"))

    def ensure(self) -> None:
        if self._ws is not None and not self._closed.is_set():
            return
        fresh = False
        with self._lock:
            if self._ws is None or self._closed.is_set():
                self._teardown_locked()
                self._connect_locked()
                fresh = True
        if fresh:
            # Outside the connect lock: send() re-enters it, and doing this
            # inside _connect_locked deadlocked the first request on boot.
            # Needed so targetCreated/targetDestroyed events arrive.
            self.send("Target.setDiscoverTargets", {"discover": True}, allow_error=True)

    def _teardown_locked(self) -> None:
        self._closed.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        with self._state_lock:
            for pend in self._pending.values():
                pend.error = {"message": "cdp connection closed"}
                pend.event.set()
            self._pending.clear()
            for waiter in self._waiters:
                waiter.event.set()
            self._waiters.clear()

    def close(self) -> None:
        with self._lock:
            self._teardown_locked()

    # -- reader --------------------------------------------------------------
    def _read_loop(self, conn: Any, closed: threading.Event) -> None:
        while not closed.is_set():
            try:
                raw = conn.recv()
            except Exception as exc:  # noqa: BLE001
                if not closed.is_set():
                    logger.info("cdp reader stopped: %s", exc)
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self._dispatch(msg)
        closed.set()
        with self._state_lock:
            for pend in self._pending.values():
                pend.error = {"message": "cdp connection lost"}
                pend.event.set()
            self._pending.clear()
            for waiter in self._waiters:
                waiter.event.set()
            self._waiters.clear()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        mid = msg.get("id")
        if mid is not None:
            with self._state_lock:
                pend = self._pending.pop(mid, None)
            if pend is not None:
                pend.error = msg.get("error")
                pend.result = msg.get("result")
                pend.event.set()
            return

        method = msg.get("method", "")
        session_id = msg.get("sessionId")
        params = msg.get("params") or {}

        # A blocking alert()/confirm() freezes the renderer; everything after it
        # would time out with no explanation. Record it so errors can say so and
        # /browser/dialog can clear it.
        if method == "Page.javascriptDialogOpening" and session_id:
            self.dialogs[session_id] = {
                "type": params.get("type"),
                "message": params.get("message", "")[:500],
                "url": params.get("url"),
                "has_prompt": params.get("type") == "prompt",
            }
        elif method == "Page.javascriptDialogClosed" and session_id:
            self.dialogs.pop(session_id, None)
        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            if sid:
                self._forget_session(sid)
        elif method == "Target.targetDestroyed":
            tid = params.get("targetId")
            with self._state_lock:
                sid = self._sessions.pop(tid, None)
            if sid:
                self._forget_session(sid)
        elif method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            sid = params.get("sessionId")
            parent = session_id
            if sid and info.get("type") == "iframe" and parent:
                # Only record it. Enabling domains here would call send() from
                # the reader thread, and send() waits for a reply that only the
                # reader thread can deliver - an instant deadlock. Domains are
                # enabled lazily by enable_domains() on a request thread.
                with self._state_lock:
                    self._session_targets[sid] = info.get("targetId", "")
                    self._iframe_sessions.setdefault(parent, set()).add(sid)

        with self._state_lock:
            hits = [
                w for w in self._waiters
                if w.method == method
                and (w.session_id is None or w.session_id == session_id)
                and w.predicate(params)
            ]
            for waiter in hits:
                waiter.payload = params
                waiter.event.set()
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass

    def _forget_session(self, session_id: str) -> None:
        with self._state_lock:
            tid = self._session_targets.pop(session_id, None)
            if tid:
                self._sessions.pop(tid, None)
            self._domains_ready.discard(session_id)
            self._iframe_sessions.pop(session_id, None)
            for kids in self._iframe_sessions.values():
                kids.discard(session_id)
            self.dialogs.pop(session_id, None)

    # -- request/response ----------------------------------------------------
    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
        allow_error: bool = False,
    ) -> dict[str, Any]:
        self.ensure()
        timeout = timeout or CDP_TIMEOUT
        pend = _Pending()
        with self._lock:
            if self._ws is None or self._closed.is_set():
                raise _unavailable("cdp socket is closed")
            self._next_id += 1
            mid = self._next_id
            payload: dict[str, Any] = {"id": mid, "method": method}
            if params:
                payload["params"] = params
            if session_id:
                payload["sessionId"] = session_id
            with self._state_lock:
                self._pending[mid] = pend
            try:
                self._ws.send(json.dumps(payload))
            except Exception as exc:  # noqa: BLE001
                with self._state_lock:
                    self._pending.pop(mid, None)
                self._teardown_locked()
                raise _unavailable(f"cdp send failed: {exc}") from exc

        if not pend.event.wait(timeout):
            with self._state_lock:
                self._pending.pop(mid, None)
            dialog = self.dialogs.get(session_id or "")
            raise BrowserError(
                "cdp_timeout",
                f"{method} did not answer in {timeout}s",
                status=504,
                pending_dialog=dialog,
                hint=(
                    "a javascript dialog is blocking the page; clear it with "
                    "POST /browser/dialog" if dialog else None
                ),
            )
        if pend.error is not None:
            msg = str(pend.error.get("message", pend.error))
            if allow_error:
                return {"__cdp_error__": msg}
            raise BrowserError("cdp_error", f"{method}: {msg}", status=502)
        return pend.result or {}

    # -- events --------------------------------------------------------------
    def wait_event(
        self,
        method: str,
        *,
        session_id: str | None = None,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        waiter = _Waiter(session_id, method, predicate or (lambda _p: True))
        with self._state_lock:
            self._waiters.append(waiter)
        try:
            if waiter.event.wait(timeout):
                return waiter.payload
            return None
        finally:
            with self._state_lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    # -- targets/sessions ----------------------------------------------------
    def page_targets(self) -> list[dict[str, Any]]:
        res = self.send("Target.getTargets")
        return [
            t for t in res.get("targetInfos", [])
            if t.get("type") == "page" and not str(t.get("url", "")).startswith("devtools://")
        ]

    def session_for(self, target_id: str) -> str:
        with self._state_lock:
            sid = self._sessions.get(target_id)
        if sid:
            return sid
        res = self.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        sid = res.get("sessionId")
        if not sid:
            raise BrowserError("attach_failed", target_id, status=502)
        with self._state_lock:
            self._sessions[target_id] = sid
            self._session_targets[sid] = target_id
        self.enable_domains(sid, page=True)
        return sid

    def enable_domains(self, session_id: str, *, page: bool) -> None:
        """Idempotently turn on the domains this module needs on a session.

        Must run on a request thread, never on the reader thread.
        Page: lifecycle + javascript dialogs. DOM: backendNodeId resolution.
        Accessibility: the snapshot itself.
        """
        with self._state_lock:
            if session_id in self._domains_ready:
                return
            self._domains_ready.add(session_id)
        for domain in ("Page.enable", "DOM.enable", "Accessibility.enable"):
            self.send(domain, session_id=session_id, timeout=10, allow_error=True)
        self.send(
            "Page.setLifecycleEventsEnabled", {"enabled": True},
            session_id=session_id, timeout=10, allow_error=True,
        )
        if page:
            # Attach to out-of-process iframes so consent banners, payment
            # fields and embedded widgets appear in the snapshot instead of
            # being a blank rectangle.
            self.send(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                session_id=session_id, timeout=10, allow_error=True,
            )

    def iframe_sessions(self, page_session: str) -> list[str]:
        with self._state_lock:
            kids = sorted(self._iframe_sessions.get(page_session, set()))
        for sid in kids:
            self.enable_domains(sid, page=False)
        return kids

    def target_of(self, session_id: str) -> str:
        with self._state_lock:
            return self._session_targets.get(session_id, "")


# --------------------------------------------------------------------------- #
# Fixed page-side functions
# --------------------------------------------------------------------------- #
# Every one of these is a constant. Caller input reaches them only through
# Runtime.callFunctionOn `arguments`, never through string concatenation.

JS_PROBE = """
function(cx, cy) {
  const el = this;
  if (!el || !el.isConnected) return {connected: false};
  const r = el.getBoundingClientRect();
  const cs = (el.nodeType === 1) ? getComputedStyle(el) : null;
  const styled = !cs || (cs.visibility !== 'hidden' && cs.display !== 'none');
  const px = (cx === null || cx === undefined) ? r.left + r.width / 2 : cx;
  const py = (cy === null || cy === undefined) ? r.top + r.height / 2 : cy;
  let hit = null, hitDesc = null;
  const inView = r.bottom > 0 && r.right > 0 &&
                 r.top < (window.innerHeight || 0) && r.left < (window.innerWidth || 0);
  if (inView && r.width > 0 && r.height > 0) {
    let top = null;
    try { top = document.elementFromPoint(px, py); } catch (e) { top = null; }
    if (top) {
      hit = (top === el) || el.contains(top) || top.contains(el);
      const lbl = (top.getAttribute && top.getAttribute('aria-label')) || '';
      hitDesc = (top.tagName || '?').toLowerCase() +
                (lbl ? ' "' + lbl.slice(0, 40) + '"'
                     : ' "' + ((top.innerText || top.textContent || '').trim().slice(0, 40)) + '"');
    }
  }
  return {
    connected: true,
    tag: (el.tagName || '').toLowerCase(),
    type: (el.getAttribute && el.getAttribute('type')) || null,
    rect: {x: r.left, y: r.top, w: r.width, h: r.height},
    visible: styled && r.width > 0 && r.height > 0,
    inViewport: inView,
    disabled: !!el.disabled || el.getAttribute && el.getAttribute('aria-disabled') === 'true',
    value: (typeof el.value === 'string') ? el.value.slice(0, 200) : null,
    hit: hit,
    hitDesc: hitDesc
  };
}
"""

JS_SELECT = """
function(values) {
  const el = this;
  if (!el || el.tagName !== 'SELECT') return {ok: false, reason: 'not_a_select'};
  const want = values.map(String);
  const chosen = [];
  for (const opt of Array.from(el.options)) {
    const match = want.includes(opt.value) || want.includes((opt.label || '').trim())
               || want.includes((opt.textContent || '').trim());
    opt.selected = match;
    if (match) chosen.push({value: opt.value, label: (opt.label || opt.textContent || '').trim()});
  }
  if (!chosen.length) {
    return {ok: false, reason: 'no_matching_option',
            options: Array.from(el.options).slice(0, 40).map(o => ({
              value: o.value, label: (o.label || o.textContent || '').trim().slice(0, 60)}))};
  }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, selected: chosen};
}
"""

JS_TEXT = """
function(selector, maxChars) {
  let root = document.body;
  if (selector) {
    root = document.querySelector(selector);
    if (!root) return {ok: false, reason: 'selector_not_found'};
  }
  const t = (root.innerText || root.textContent || '').replace(/\\n{3,}/g, '\\n\\n').trim();
  return {ok: true, length: t.length, truncated: t.length > maxChars, text: t.slice(0, maxChars)};
}
"""

JS_EXTRACT = """
function(rowSelector, fields, limit) {
  let rows;
  try { rows = Array.from(document.querySelectorAll(rowSelector)); }
  catch (e) { return {ok: false, reason: 'bad_selector', detail: String(e.message || e)}; }
  const total = rows.length;
  const out = [];
  for (const row of rows.slice(0, limit)) {
    if (!fields || !fields.length) {
      out.push({text: (row.innerText || '').trim().slice(0, 400)});
      continue;
    }
    const rec = {};
    for (const f of fields) {
      let node = row;
      if (f.selector) { try { node = row.querySelector(f.selector); } catch (e) { node = null; } }
      if (!node) { rec[f.name] = null; continue; }
      if (f.attr) rec[f.name] = node.getAttribute(f.attr);
      else if (f.attr === '' || f.attr === null || f.attr === undefined)
        rec[f.name] = (node.innerText || node.textContent || '').trim().slice(0, 400);
    }
    out.push(rec);
  }
  return {ok: true, total: total, returned: out.length, rows: out};
}
"""

JS_WAIT = """
function(selector, mode, text) {
  let el = null;
  if (selector) {
    try { el = document.querySelector(selector); } catch (e) { return {error: 'bad_selector'}; }
  }
  if (mode === 'attached')  return {done: !!el};
  if (mode === 'detached')  return {done: !el};
  if (mode === 'visible' || mode === 'hidden') {
    let vis = false;
    if (el) {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      vis = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
    }
    return {done: (mode === 'visible') ? vis : !vis};
  }
  if (mode === 'text') {
    const hay = (document.body && (document.body.innerText || '')) || '';
    return {done: hay.indexOf(text) !== -1};
  }
  if (mode === 'load') return {done: document.readyState === 'complete',
                               state: document.readyState};
  return {error: 'bad_mode'};
}
"""

JS_SCROLL_WINDOW = """
function(dx, dy) {
  window.scrollBy(dx, dy);
  return {x: window.scrollX, y: window.scrollY,
          maxY: Math.max(0, document.documentElement.scrollHeight - window.innerHeight)};
}
"""


# --------------------------------------------------------------------------- #
# Snapshot model
# --------------------------------------------------------------------------- #
# AX roles a human can interact with. Chrome mixes ARIA role names (lowercase)
# with internal Blink names (CamelCase), so both spellings live here.
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "slider", "spinbutton", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab", "treeitem", "menubutton",
    "disclosuretriangle", "colorwell", "datetime", "textfield", "popupbutton",
    "togglebutton", "menulistoption",
}
# Containers Chrome inserts around real controls (the popup wrapper of a native
# <select>, scrollbars). Traversed, never given a ref - a model that "clicks the
# menulistpopup" has been handed a target that does nothing.
CONTAINER_ROLES = {"menulistpopup", "scrollbar"}
STRUCTURAL_ROLES = {"heading", "dialog", "alertdialog", "alert", "tabpanel", "form", "search"}
TEXT_ROLES = {"statictext", "text", "paragraph", "label", "listitem", "cell", "columnheader", "rowheader"}
SKIP_ROLES = {"inlinetextbox", "linebreak", "generic", "none", "presentation", "ignored"}

FLAG_PROPS = {
    "disabled": "disabled",
    "checked": "checked",
    "expanded": "expanded",
    "required": "required",
    "focused": "focused",
    "selected": "selected",
    "invalid": "invalid",
    "pressed": "pressed",
    "readonly": "readonly",
}


@dataclass
class ElementRef:
    ref: str
    session_id: str
    target_id: str
    backend_node_id: int
    role: str
    name: str
    tag: str
    attr_key: str          # cheap secondary identity when the AX name is empty
    snapshot_id: str
    loader_id: str
    frame_url: str = ""


@dataclass
class Snapshot:
    snapshot_id: str
    target_id: str
    loader_id: str
    url: str
    created: float
    refs: dict[str, ElementRef]


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _clip(text: str, limit: int) -> str:
    text = _norm(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
class BrowserController:
    def __init__(self) -> None:
        self.cdp = CDPConnection()
        self._lock = threading.RLock()
        self._active_target: str | None = None
        self._snapshots: dict[str, Snapshot] = {}
        self._refs: dict[str, ElementRef] = {}
        self._snap_seq = 0
        self._ref_seq = 0

    # -- targets -------------------------------------------------------------
    def targets(self) -> list[dict[str, Any]]:
        return self.cdp.page_targets()

    def active_target(self, target_id: str | None = None) -> str:
        pages = self.targets()
        if not pages:
            # No tab at all: open one rather than failing, so the very first
            # navigate after a cold boot works.
            res = self.cdp.send("Target.createTarget", {"url": "about:blank"})
            tid = res.get("targetId")
            if not tid:
                raise BrowserError("no_page_target", "browser has no page target", status=503)
            self._active_target = tid
            return tid
        ids = {p["targetId"] for p in pages}
        if target_id:
            if target_id not in ids:
                raise BrowserError(
                    "unknown_target", target_id, status=409,
                    known=[p["targetId"] for p in pages],
                )
            self._active_target = target_id
            return target_id
        if self._active_target in ids:
            return self._active_target  # type: ignore[return-value]
        self._active_target = pages[0]["targetId"]
        return self._active_target

    def session(self, target_id: str | None = None) -> tuple[str, str]:
        tid = self.active_target(target_id)
        return tid, self.cdp.session_for(tid)

    def loader_id(self, session_id: str) -> str:
        tree = self.cdp.send("Page.getFrameTree", session_id=session_id, timeout=10)
        return (((tree.get("frameTree") or {}).get("frame")) or {}).get("loaderId", "")

    def frame_info(self, session_id: str) -> dict[str, Any]:
        tree = self.cdp.send("Page.getFrameTree", session_id=session_id, timeout=10)
        return ((tree.get("frameTree") or {}).get("frame")) or {}

    # -- navigation ----------------------------------------------------------
    def navigate(
        self, url: str, target_id: str | None, wait_until: str, timeout: float
    ) -> dict[str, Any]:
        if not ALLOWED_URL_RE.match(url):
            raise BrowserError(
                "url_not_allowed",
                "only http(s), about:blank and file:///home/nesq/ are accepted",
            )
        tid, sid = self.session(target_id)
        started = time.time()
        res = self.cdp.send("Page.navigate", {"url": url}, session_id=sid, timeout=timeout)
        if res.get("errorText"):
            self._invalidate(tid)
            raise BrowserError(
                "navigation_failed", f"{url}: {res['errorText']}", status=502,
                url=url,
            )
        self._invalidate(tid)
        state = self._await_load(sid, wait_until, timeout - (time.time() - started))
        return {**self._page_meta(sid), "target_id": tid, "load_state": state,
                "took_ms": round((time.time() - started) * 1000, 1)}

    def _await_load(self, session_id: str, wait_until: str, budget: float) -> str:
        budget = max(1.0, budget)
        if wait_until == "none":
            return "none"
        want = {"load": "load", "domcontentloaded": "DOMContentLoaded",
                "networkidle": "networkIdle"}.get(wait_until, "load")
        got = self.cdp.wait_event(
            "Page.lifecycleEvent",
            session_id=session_id,
            predicate=lambda p: p.get("name") == want,
            timeout=budget,
        )
        if got is not None:
            return wait_until
        # Not fatal: plenty of real pages never go network-idle. Say so instead
        # of pretending the wait succeeded.
        return f"timeout:{wait_until}"

    def _page_meta(self, session_id: str) -> dict[str, Any]:
        frame = self.frame_info(session_id)
        title = ""
        try:
            res = self.cdp.send(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
                session_id=session_id, timeout=10, allow_error=True,
            )
            title = ((res.get("result") or {}).get("value")) or ""
        except BrowserError:
            pass
        return {"ok": True, "url": frame.get("url", ""), "title": _clip(title, 200),
                "loader_id": frame.get("loaderId", "")}

    def history(self, session_id: str, delta: int) -> dict[str, Any]:
        hist = self.cdp.send("Page.getNavigationHistory", session_id=session_id, timeout=10)
        entries = hist.get("entries", [])
        idx = hist.get("currentIndex", 0) + delta
        if idx < 0 or idx >= len(entries):
            raise BrowserError(
                "no_history_entry",
                f"cannot move {delta:+d} from entry {hist.get('currentIndex')} of {len(entries)}",
                status=409,
            )
        self.cdp.send(
            "Page.navigateToHistoryEntry", {"entryId": entries[idx]["id"]},
            session_id=session_id, timeout=NAV_TIMEOUT,
        )
        self._invalidate(self.cdp.target_of(session_id))
        self._await_load(session_id, "load", 15)
        return self._page_meta(session_id)

    # -- ref bookkeeping -----------------------------------------------------
    def _invalidate(self, target_id: str) -> None:
        """Drop every ref for a target. Called on navigation."""
        with self._lock:
            dead = [s for s in self._snapshots.values() if s.target_id == target_id]
            for snap in dead:
                self._snapshots.pop(snap.snapshot_id, None)
                for ref in snap.refs:
                    self._refs.pop(ref, None)

    def _store(self, snap: Snapshot) -> None:
        with self._lock:
            self._snapshots[snap.snapshot_id] = snap
            self._refs.update(snap.refs)
            # Keep only the most recent SNAPSHOT_KEEP snapshots resolvable, so a
            # long session cannot grow this map without bound.
            if len(self._snapshots) > SNAPSHOT_KEEP:
                old = sorted(self._snapshots.values(), key=lambda s: s.created)
                for stale in old[: len(self._snapshots) - SNAPSHOT_KEEP]:
                    self._snapshots.pop(stale.snapshot_id, None)
                    for ref in stale.refs:
                        self._refs.pop(ref, None)

    # -- snapshot ------------------------------------------------------------
    def snapshot(
        self,
        *,
        target_id: str | None,
        max_elements: int,
        include_text: bool,
        text_limit: int,
        name_filter: str | None,
        role_filter: str | None,
        viewport_only: bool,
        fmt: str,
    ) -> dict[str, Any]:
        tid, sid = self.session(target_id)
        frame = self.frame_info(sid)
        loader = frame.get("loaderId", "")

        with self._lock:
            self._snap_seq += 1
            snapshot_id = f"s{self._snap_seq}"

        sessions: list[tuple[str, str]] = [(sid, "")]
        for child in self.cdp.iframe_sessions(sid):
            sessions.append((child, self._session_url(child)))

        rows: list[dict[str, Any]] = []
        refs: dict[str, ElementRef] = {}
        total_matched = 0
        total_interactive = 0
        text_used = 0

        # A wall-clock budget for the WHOLE snapshot, not per CDP call.
        #
        # `CDP_TIMEOUT` bounds one round trip; a snapshot makes one
        # `Accessibility.getFullAXTree` per frame, and a heavy SPA has many. On
        # LinkedIn that compounded into a single `browser_snapshot` taking 1m05s
        # while the same call on a light page takes 168ms — the agent spent more
        # time reading one page than a model call costs by a factor of thirty.
        #
        # Stopping early is not a loss of correctness: `truncated` already exists
        # in this contract, the renderer already tells the model to narrow with
        # `name_filter`/`role_filter`, and uniqueness claims already refuse to
        # resolve against a truncated snapshot. A partial answer in eight seconds
        # that says it is partial beats a complete one after a minute.
        deadline = time.monotonic() + SNAPSHOT_BUDGET_SECONDS
        frames_skipped_for_time = 0

        for sess, frame_url in sessions:
            if time.monotonic() > deadline:
                frames_skipped_for_time += len(sessions) - sessions.index((sess, frame_url))
                logger.info(
                    "snapshot: %ss budget spent, %d frame(s) unread",
                    SNAPSHOT_BUDGET_SECONDS, frames_skipped_for_time,
                )
                break
            try:
                collected, matched, seen = self._collect_session(
                    sess, tid, snapshot_id, loader, frame_url,
                    include_text=include_text,
                    text_budget=max(0, text_limit - text_used),
                    name_filter=name_filter,
                    role_filter=role_filter,
                    viewport_only=viewport_only,
                    remaining=max(0, max_elements - len(refs)),
                    refs=refs,
                )
            except BrowserError as exc:
                logger.info("snapshot: skipping frame %s (%s)", frame_url or "main", exc.code)
                continue
            total_matched += matched
            total_interactive += seen
            text_used += sum(1 for r in collected if r["kind"] == "text")
            if frame_url and collected:
                rows.append({"kind": "frame", "depth": 0, "url": frame_url})
            rows.extend(collected)

        rows = self._dedupe(rows)
        snap = Snapshot(snapshot_id, tid, loader, frame.get("url", ""), time.time(), refs)
        self._store(snap)

        returned = len(refs)
        # Running out of time is a kind of truncation, and has to be reported as
        # one: a model that believes it saw the whole page will conclude an
        # element is absent when it was merely unread. `resolve_approved` and the
        # stale-ref recovery both refuse to claim uniqueness against a truncated
        # snapshot, and that refusal is only correct if this flag is honest.
        truncated = total_matched > returned or frames_skipped_for_time > 0
        meta = self._page_meta(sid)
        payload: dict[str, Any] = {
            "ok": True,
            "snapshot_id": snapshot_id,
            "target_id": tid,
            "url": meta["url"],
            "title": meta["title"],
            "interactive_total": total_interactive,
            "matched": total_matched,
            "returned": returned,
            "truncated": truncated,
            "frames": len(sessions),
        }
        if frames_skipped_for_time:
            payload["frames_unread"] = frames_skipped_for_time
            payload["budget_exceeded"] = True

        if name_filter or role_filter:
            payload["filters"] = {"name_filter": name_filter, "role_filter": role_filter}
        if self.cdp.dialogs.get(sid):
            payload["pending_dialog"] = self.cdp.dialogs[sid]

        if fmt == "json":
            payload["elements"] = [
                {k: v for k, v in r.items() if k != "kind" and v not in (None, "", [], {})}
                | {"kind": r["kind"]}
                for r in rows
            ]
            if truncated:
                payload["truncation_note"] = (
                    f"{total_matched - returned} more interactive elements were not returned; "
                    "raise max_elements or narrow with name_filter/role_filter"
                )
        else:
            text, hard_clipped = self._render(rows, truncated, total_matched, returned)
            payload["format"] = "lines"
            payload["snapshot"] = text
            payload["bytes"] = len(text.encode("utf-8"))
            if hard_clipped:
                payload["byte_capped"] = True
        return payload

    def _session_url(self, session_id: str) -> str:
        try:
            return self.frame_info(session_id).get("url", "") or "(iframe)"
        except BrowserError:
            return "(iframe)"

    def _collect_session(  # noqa: C901 - one flat pass over the AX tree
        self,
        session_id: str,
        target_id: str,
        snapshot_id: str,
        loader_id: str,
        frame_url: str,
        *,
        include_text: bool,
        text_budget: int,
        name_filter: str | None,
        role_filter: str | None,
        viewport_only: bool,
        remaining: int,
        refs: dict[str, ElementRef],
    ) -> tuple[list[dict[str, Any]], int, int]:
        tree = self.cdp.send(
            "Accessibility.getFullAXTree", {}, session_id=session_id, timeout=CDP_TIMEOUT
        )
        nodes = tree.get("nodes", [])
        if not nodes:
            return [], 0, 0
        by_id = {n["nodeId"]: n for n in nodes}
        roots = [n for n in nodes if not n.get("parentId")]
        if not roots:
            roots = nodes[:1]

        attrs = self._dom_attributes(session_id)
        viewport = self._viewport(session_id) if viewport_only else None

        rows: list[dict[str, Any]] = []
        matched = 0        # candidates after filtering
        seen_total = 0     # every interactive/structural node in the frame
        emitted = 0
        text_left = text_budget
        name_needle = (name_filter or "").lower() or None
        role_needle = (role_filter or "").lower() or None

        # (ax node id, indent depth, suppress_text). suppress_text is inherited
        # by the subtree of any control we chose NOT to emit: without it, a
        # filtered-out or truncated button still leaks its StaticText child and
        # the caller gets 400 lines of label text for elements it cannot act on.
        stack: list[tuple[str, int, bool]] = [(r["nodeId"], 0, False) for r in reversed(roots)]
        while stack:
            node_id, depth, suppressed = stack.pop()
            node = by_id.get(node_id)
            if node is None:
                continue
            children = list(node.get("childIds", []))
            emit_depth = depth
            child_suppressed = suppressed

            role = _norm(((node.get("role") or {}).get("value")) or "").lower()
            ignored = bool(node.get("ignored"))
            backend = node.get("backendDOMNodeId")
            name = _clip(((node.get("name") or {}).get("value")) or "", SNAPSHOT_NAME_CHARS)
            value = _clip(str(((node.get("value") or {}).get("value")) or ""), 80)

            if role in CONTAINER_ROLES:
                for child_id in reversed(children):
                    stack.append((child_id, depth, suppressed))
                continue

            interactive = (not ignored) and role in INTERACTIVE_ROLES
            structural = (not ignored) and role in STRUCTURAL_ROLES
            is_text = (not ignored) and role in TEXT_ROLES

            if interactive or structural:
                seen_total += 1
                keep = bool(backend)
                if keep and name_needle:
                    keep = name_needle in name.lower() or name_needle in value.lower()
                if keep and role_needle:
                    keep = role_needle in role
                if keep and viewport and backend:
                    keep = self._maybe_in_viewport(session_id, backend, viewport)
                if keep:
                    matched += 1
                if keep and emitted < remaining:
                    emitted += 1
                    with self._lock:
                        self._ref_seq += 1
                        ref = f"e{self._ref_seq}"
                    meta = attrs.get(backend, {})
                    tag = meta.get("tag", "")
                    flags = self._flags(node)
                    row: dict[str, Any] = {
                        "kind": "el",
                        "depth": min(emit_depth, 6),
                        "ref": ref,
                        "role": role,
                        "name": name,
                        "value": value or (meta.get("value") if role in
                                           ("textbox", "searchbox", "combobox") else ""),
                        "tag": tag,
                        "flags": flags,
                    }
                    href = meta.get("href")
                    if href and role == "link":
                        row["href"] = _clip(href, 120)
                    ph = meta.get("placeholder")
                    if ph and not name:
                        row["name"] = _clip(ph, SNAPSHOT_NAME_CHARS)
                        row["placeholder"] = True
                    rows.append(row)
                    refs[ref] = ElementRef(
                        ref=ref,
                        session_id=session_id,
                        target_id=target_id,
                        backend_node_id=backend,
                        role=role,
                        name=row["name"],
                        tag=tag,
                        attr_key=meta.get("key", ""),
                        snapshot_id=snapshot_id,
                        loader_id=loader_id,
                        frame_url=frame_url,
                    )
                    emit_depth = depth + 1
                else:
                    # We skipped a real control: everything under it is label
                    # text for something the caller cannot reference.
                    child_suppressed = True
            elif is_text and include_text and text_left > 0 and not suppressed:
                txt = _clip(name or value, SNAPSHOT_TEXT_CHARS)
                if len(txt) >= 3:
                    rows.append({"kind": "text", "depth": min(emit_depth, 6), "text": txt})
                    text_left -= 1
                    emit_depth = depth + 1
            elif not ignored and role not in SKIP_ROLES:
                emit_depth = depth  # keep indentation meaningful

            for child_id in reversed(children):
                stack.append((child_id, emit_depth, child_suppressed))

        return rows, matched, seen_total

    @staticmethod
    def _flags(node: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for prop in node.get("properties", []) or []:
            key = str(prop.get("name", "")).lower()
            if key not in FLAG_PROPS:
                continue
            raw = (prop.get("value") or {}).get("value")
            if raw in (True, "true"):
                out.append(FLAG_PROPS[key])
            elif key == "checked" and raw in ("mixed",):
                out.append("mixed")
            elif key in ("invalid",) and raw not in (False, "false", None):
                out.append("invalid")
        return out

    def _dom_attributes(self, session_id: str) -> dict[int, dict[str, Any]]:
        """backendNodeId -> {tag, href, placeholder, value, key}.

        One ``DOM.getDocument`` gives tag names and attributes for the whole
        tree in a single round trip. It never leaves the sidecar; it exists so
        the rendered snapshot can carry link targets and so a stale ref has a
        second identity signal beyond the accessible name.
        """
        try:
            doc = self.cdp.send(
                "DOM.getDocument", {"depth": -1, "pierce": True},
                session_id=session_id, timeout=CDP_TIMEOUT,
            )
        except BrowserError:
            return {}
        out: dict[int, dict[str, Any]] = {}
        stack = [doc.get("root") or {}]
        seen = 0
        while stack and seen < 50000:
            node = stack.pop()
            seen += 1
            backend = node.get("backendNodeId")
            if backend:
                flat = node.get("attributes") or []
                amap = {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
                out[backend] = {
                    "tag": str(node.get("nodeName", "")).lower(),
                    "href": amap.get("href"),
                    "placeholder": amap.get("placeholder"),
                    "value": amap.get("value"),
                    # A cheap secondary identity for elements whose accessible
                    # name is empty (icon buttons, bare inputs).
                    "key": "|".join(
                        filter(None, (
                            amap.get("id"), amap.get("name"), amap.get("type"),
                            amap.get("data-testid"),
                        ))
                    ),
                }
            for key in ("children", "shadowRoots", "pseudoElements"):
                stack.extend(node.get(key) or [])
            if node.get("contentDocument"):
                stack.append(node["contentDocument"])
        return out

    def _viewport(self, session_id: str) -> dict[str, float]:
        try:
            m = self.cdp.send("Page.getLayoutMetrics", session_id=session_id, timeout=10)
        except BrowserError:
            return {}
        vv = m.get("cssVisualViewport") or m.get("visualViewport") or {}
        return {"w": float(vv.get("clientWidth", 0)), "h": float(vv.get("clientHeight", 0))}

    def _maybe_in_viewport(
        self, session_id: str, backend: int, viewport: dict[str, float]
    ) -> bool:
        quad = self._quads(session_id, backend, quiet=True)
        if not quad:
            return False
        xs = quad[0::2]
        ys = quad[1::2]
        return (max(xs) > 0 and max(ys) > 0
                and min(xs) < viewport.get("w", 1e9)
                and min(ys) < viewport.get("h", 1e9))

    def _quads(self, session_id: str, backend: int, *, quiet: bool = False) -> list[float]:
        res = self.cdp.send(
            "DOM.getContentQuads", {"backendNodeId": backend},
            session_id=session_id, timeout=10, allow_error=True,
        )
        if "__cdp_error__" in res:
            if quiet:
                return []
            return []
        quads = res.get("quads") or []
        return list(quads[0]) if quads else []

    # -- rendering -----------------------------------------------------------
    @staticmethod
    def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop static text that only repeats a neighbouring control's name.

        The AX tree gives a button both a name and a StaticText child with the
        same string, and a <label> shows up next to the input it names. Emitting
        both roughly doubles the snapshot for zero information. This is pure
        byte-shaving: it never removes a ref.
        """
        def label(row: dict[str, Any] | None) -> str:
            if not row:
                return "\x00"
            if row["kind"] == "el":
                return _norm(row.get("name", "")).casefold()
            if row["kind"] == "text":
                return _norm(row.get("text", "")).casefold()
            return "\x00"

        out: list[dict[str, Any]] = []
        for idx, row in enumerate(rows):
            if row["kind"] == "text":
                mine = label(row)
                # Compare against what actually survived, not the original
                # previous row - otherwise a duplicated pair deletes itself
                # entirely instead of collapsing to one copy.
                if mine and (
                    mine == label(out[-1] if out else None)
                    or mine == label(rows[idx + 1] if idx + 1 < len(rows) else None)
                ):
                    continue
            out.append(row)
        return out

    @staticmethod
    def _render(
        rows: list[dict[str, Any]], truncated: bool, total: int, returned: int
    ) -> tuple[str, bool]:
        lines: list[str] = []
        for row in rows:
            pad = "  " * int(row.get("depth", 0))
            if row["kind"] == "frame":
                lines.append(f"{pad}--- iframe {row['url']} ---")
                continue
            if row["kind"] == "text":
                lines.append(f'{pad}text "{row["text"]}"')
                continue
            parts = [f"{pad}{row['ref']}", row["role"]]
            if row.get("name"):
                parts.append(f'"{row["name"]}"')
            if row.get("value"):
                parts.append(f'value="{row["value"]}"')
            if row.get("href"):
                parts.append(f"-> {row['href']}")
            if row.get("flags"):
                parts.append("[" + " ".join(row["flags"]) + "]")
            lines.append(" ".join(parts))

        if truncated:
            lines.append(
                f"... {total - returned} more interactive elements not shown "
                "(raise max_elements, or narrow with name_filter / role_filter)"
            )
        text = "\n".join(lines)
        hard = False
        if len(text.encode("utf-8")) > SNAPSHOT_MAX_BYTES:
            hard = True
            encoded = text.encode("utf-8")[:SNAPSHOT_MAX_BYTES]
            text = encoded.decode("utf-8", "ignore")
            text = text[: text.rfind("\n")] if "\n" in text else text
            text += (
                f"\n... snapshot hit the {SNAPSHOT_MAX_BYTES} byte cap and was cut here; "
                "narrow it with name_filter / role_filter"
            )
        return text, hard

    # -- ref resolution ------------------------------------------------------
    def resolve(self, ref: str, snapshot_id: str | None, *, force: bool = False) -> tuple[ElementRef, dict[str, Any]]:
        """Turn a ref into a live, verified element - or raise.

        The contract the API lane depends on: this either returns the *same*
        element the snapshot described, or it raises. It never silently returns
        a different one.
        """
        with self._lock:
            entry = self._refs.get(ref)
        if entry is None:
            raise BrowserError(
                "unknown_ref", f"{ref} is not from a live snapshot", status=409,
                hint="call POST /browser/snapshot and use a ref from that response",
            )
        if snapshot_id and snapshot_id != entry.snapshot_id:
            raise BrowserError(
                "stale_ref", f"{ref} belongs to snapshot {entry.snapshot_id}, not {snapshot_id}",
                status=409, snapshot_id=entry.snapshot_id,
            )

        live_pages = {p["targetId"] for p in self.targets()}
        if entry.target_id not in live_pages:
            raise BrowserError("stale_ref", f"the tab {ref} came from is gone", status=409)

        # A ref names an element in a specific tab. If that tab is in the
        # background, bring it forward first: Chromium throttles background
        # renderers, so the same click that takes 150ms here took 5s there, and
        # on real sites input to a hidden tab can be dropped outright.
        switched: str | None = None
        if entry.target_id != self._active_target:
            self.cdp.send(
                "Target.activateTarget", {"targetId": entry.target_id}, allow_error=True
            )
            self._active_target = entry.target_id
            switched = entry.target_id

        page_session = self.cdp.session_for(entry.target_id)
        current_loader = self.loader_id(page_session)
        if current_loader != entry.loader_id:
            self._invalidate(entry.target_id)
            raise BrowserError(
                "stale_ref",
                f"the page navigated since snapshot {entry.snapshot_id} was taken",
                status=409, hint="re-snapshot before acting",
            )

        probe = self._probe(entry)
        if not probe.get("connected"):
            raise BrowserError(
                "stale_ref", f"{ref} is no longer in the document", status=409,
                expected={"role": entry.role, "name": entry.name},
            )

        # Identity re-check against Chrome's own accessibility computation.
        role, name = self._ax_identity(entry)
        if role != entry.role or _norm(name) != _norm(entry.name):
            if not force:
                raise BrowserError(
                    "stale_ref",
                    f"{ref} now resolves to a different element",
                    status=409,
                    expected={"role": entry.role, "name": entry.name},
                    actual={"role": role, "name": name},
                    hint="re-snapshot; pass force=true only if you are sure",
                )
            logger.warning(
                "force acting on changed ref %s expected=%r/%r actual=%r/%r",
                ref, entry.role, entry.name, role, name,
            )
        if not entry.name and entry.attr_key:
            tag = probe.get("tag") or ""
            if entry.tag and tag and tag != entry.tag and not force:
                raise BrowserError(
                    "stale_ref", f"{ref} changed tag {entry.tag} -> {tag}", status=409,
                )
        if switched:
            probe["switched_to_tab"] = switched
        return entry, probe

    def _probe(self, entry: ElementRef, cx: float | None = None, cy: float | None = None) -> dict[str, Any]:
        obj = self.cdp.send(
            "DOM.resolveNode", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id, timeout=10, allow_error=True,
        )
        if "__cdp_error__" in obj:
            return {"connected": False, "reason": obj["__cdp_error__"]}
        object_id = (obj.get("object") or {}).get("objectId")
        if not object_id:
            return {"connected": False, "reason": "no object id"}
        try:
            res = self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": JS_PROBE,
                    "arguments": [{"value": cx}, {"value": cy}],
                    "returnByValue": True,
                },
                session_id=entry.session_id, timeout=10, allow_error=True,
            )
        finally:
            self.cdp.send(
                "Runtime.releaseObject", {"objectId": object_id},
                session_id=entry.session_id, timeout=5, allow_error=True,
            )
        if "__cdp_error__" in res:
            return {"connected": False, "reason": res["__cdp_error__"]}
        return (res.get("result") or {}).get("value") or {"connected": False}

    def _call_on(self, entry: ElementRef, fn: str, args: list[Any]) -> Any:
        obj = self.cdp.send(
            "DOM.resolveNode", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id, timeout=10, allow_error=True,
        )
        if "__cdp_error__" in obj:
            raise BrowserError("stale_ref", obj["__cdp_error__"], status=409)
        object_id = (obj.get("object") or {}).get("objectId")
        try:
            res = self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": fn,
                    "arguments": [{"value": a} for a in args],
                    "returnByValue": True,
                },
                session_id=entry.session_id, timeout=CDP_TIMEOUT,
            )
        finally:
            self.cdp.send(
                "Runtime.releaseObject", {"objectId": object_id},
                session_id=entry.session_id, timeout=5, allow_error=True,
            )
        return (res.get("result") or {}).get("value")

    def call_global(self, session_id: str, fn: str, args: list[Any]) -> Any:
        """Run one of this module's fixed functions with ``this === globalThis``.

        ``Runtime.callFunctionOn`` needs an object to bind, and binding the
        page's global is how caller input stays *arguments* instead of being
        concatenated into an expression string. Nothing here ever builds
        JavaScript from request data.
        """
        res = self.cdp.send(
            "Runtime.evaluate", {"expression": "globalThis", "returnByValue": False},
            session_id=session_id, timeout=10,
        )
        object_id = (res.get("result") or {}).get("objectId")
        if not object_id:
            raise BrowserError("no_execution_context", "page has no global object", status=502)
        try:
            out = self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": fn,
                    "arguments": [{"value": a} for a in args],
                    "returnByValue": True,
                },
                session_id=session_id, timeout=CDP_TIMEOUT,
            )
        finally:
            self.cdp.send("Runtime.releaseObject", {"objectId": object_id},
                          session_id=session_id, timeout=5, allow_error=True)
        return (out.get("result") or {}).get("value")

    def _ax_identity(self, entry: ElementRef) -> tuple[str, str]:
        res = self.cdp.send(
            "Accessibility.getPartialAXTree",
            {"backendNodeId": entry.backend_node_id, "fetchRelatives": False},
            session_id=entry.session_id, timeout=10, allow_error=True,
        )
        if "__cdp_error__" in res:
            return ("", "")
        for node in res.get("nodes", []):
            if node.get("backendDOMNodeId") == entry.backend_node_id:
                role = _norm(((node.get("role") or {}).get("value")) or "").lower()
                name = _clip(((node.get("name") or {}).get("value")) or "", SNAPSHOT_NAME_CHARS)
                return role, name
        return ("", "")

    # -- geometry ------------------------------------------------------------
    def point_for(self, entry: ElementRef, probe: dict[str, Any], *, force: bool) -> tuple[float, float]:
        """Main-frame viewport coordinates for a click.

        ``DOM.getContentQuads`` returns coordinates in the *top* frame's space
        even when the node lives in an out-of-process iframe, which is what
        lets one Input dispatch on the page session hit an element inside a
        cross-origin frame.
        """
        self.cdp.send(
            "DOM.scrollIntoViewIfNeeded", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id, timeout=10, allow_error=True,
        )
        quad = self._quads(entry.session_id, entry.backend_node_id)
        if not quad:
            raise BrowserError(
                "not_actionable",
                f"{entry.ref} has no rendered box (display:none, zero size or detached)",
                status=409,
            )
        xs, ys = quad[0::2], quad[1::2]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)

        # scrollIntoViewIfNeeded cannot rescue an element that is deliberately
        # parked outside the viewport - skip links, 1x1 screen-reader helpers,
        # off-canvas menus. Those have a box and a positive size, so every other
        # check passes and the click lands on nothing at all.
        page_session = self.cdp.session_for(entry.target_id)
        vp = self._viewport(page_session)
        if vp and not force:
            if not (0 <= cx <= vp.get("w", 0) and 0 <= cy <= vp.get("h", 0)):
                raise BrowserError(
                    "not_actionable",
                    f"{entry.ref} sits outside the viewport at ({cx:.0f}, {cy:.0f}); "
                    f"viewport is {vp.get('w', 0):.0f}x{vp.get('h', 0):.0f}",
                    status=409,
                    hint="scroll the page, or the element is positioned off-canvas on purpose",
                )

        recheck = self._probe(entry)
        if not recheck.get("visible") and not force:
            raise BrowserError(
                "not_actionable", f"{entry.ref} is not visible", status=409,
                rect=recheck.get("rect"),
            )
        if recheck.get("disabled") and not force:
            raise BrowserError("not_actionable", f"{entry.ref} is disabled", status=409)
        if recheck.get("hit") is False and not force:
            raise BrowserError(
                "obscured",
                f"{entry.ref} is covered by {recheck.get('hitDesc') or 'another element'}",
                status=409,
                hint="dismiss the overlay (cookie banner, modal) or pass force=true",
            )
        return cx, cy

    # -- input ---------------------------------------------------------------
    def click(
        self, entry: ElementRef, probe: dict[str, Any], *,
        button: str, click_count: int, modifiers: int, force: bool,
    ) -> dict[str, Any]:
        cx, cy = self.point_for(entry, probe, force=force)
        page_session = self.cdp.session_for(entry.target_id)
        before = {p["targetId"] for p in self.targets()}
        common = {"x": cx, "y": cy, "button": button, "clickCount": click_count,
                  "modifiers": modifiers, "buttons": {"left": 1, "right": 2, "middle": 4}.get(button, 1)}
        self.cdp.send("Input.dispatchMouseEvent",
                      {"type": "mouseMoved", **{**common, "button": "none", "buttons": 0, "clickCount": 0}},
                      session_id=page_session, timeout=10)
        try:
            self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", **common},
                          session_id=page_session, timeout=10)
            self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **common},
                          session_id=page_session, timeout=10)
        except BrowserError as exc:
            # An onclick that calls alert()/confirm() freezes the renderer
            # mid-dispatch. The click DID land, so reporting a failure would
            # make the caller retry and double-fire it. Say what is blocking.
            dialog = exc.extra.get("pending_dialog")
            if exc.code == "cdp_timeout" and dialog:
                return {
                    "x": round(cx, 1), "y": round(cy, 1), "new_tabs": [],
                    "pending_dialog": dialog,
                    "note": "the click opened a javascript dialog which is now "
                            "blocking the page; clear it with POST /browser/dialog",
                }
            raise
        time.sleep(0.15)
        after = {p["targetId"] for p in self.targets()}
        opened = sorted(after - before)
        return {"x": round(cx, 1), "y": round(cy, 1), "new_tabs": opened}

    def focus(self, entry: ElementRef) -> None:
        res = self.cdp.send(
            "DOM.focus", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id, timeout=10, allow_error=True,
        )
        if "__cdp_error__" in res:
            raise BrowserError(
                "not_actionable", f"{entry.ref} cannot take focus: {res['__cdp_error__']}",
                status=409,
            )

    def type_text(
        self, entry: ElementRef, text: str, *, clear: bool, mode: str, delay_ms: int
    ) -> dict[str, Any]:
        page_session = self.cdp.session_for(entry.target_id)
        self.cdp.send(
            "DOM.scrollIntoViewIfNeeded", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id, timeout=10, allow_error=True,
        )
        self.focus(entry)
        if clear:
            self._press(page_session, "a", modifiers=2)   # ctrl+a
            self._press(page_session, "Delete")
        if mode == "auto":
            mode = "keys" if len(text) <= 200 else "insert"
        if mode == "insert":
            self.cdp.send("Input.insertText", {"text": text},
                          session_id=page_session, timeout=CDP_TIMEOUT)
        else:
            for ch in text:
                if ch == "\n":
                    self._press(page_session, "Enter")
                    continue
                self.cdp.send(
                    "Input.dispatchKeyEvent",
                    {"type": "keyDown", "text": ch, "unmodifiedText": ch, "key": ch},
                    session_id=page_session, timeout=10,
                )
                self.cdp.send(
                    "Input.dispatchKeyEvent", {"type": "keyUp", "key": ch},
                    session_id=page_session, timeout=10,
                )
                if delay_ms:
                    time.sleep(delay_ms / 1000)
        return {"chars": len(text), "mode": mode}

    # key -> (windowsVirtualKeyCode, code)
    KEYS: dict[str, tuple[int, str]] = {
        "Enter": (13, "Enter"), "Tab": (9, "Tab"), "Escape": (27, "Escape"),
        "Backspace": (8, "Backspace"), "Delete": (46, "Delete"),
        "ArrowUp": (38, "ArrowUp"), "ArrowDown": (40, "ArrowDown"),
        "ArrowLeft": (37, "ArrowLeft"), "ArrowRight": (39, "ArrowRight"),
        "Home": (36, "Home"), "End": (35, "End"),
        "PageUp": (33, "PageUp"), "PageDown": (34, "PageDown"),
        "Space": (32, "Space"), "a": (65, "KeyA"),
    }

    def _press(self, page_session: str, key: str, modifiers: int = 0) -> None:
        vk, code = self.KEYS.get(key, (0, key))
        base = {"modifiers": modifiers, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
                "key": " " if key == "Space" else key, "code": code}
        if key == "Enter":
            base["text"] = "\r"
        elif key == "Space":
            base["text"] = " "
        elif len(key) == 1 and modifiers == 0:
            base["text"] = key
        self.cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **base},
                      session_id=page_session, timeout=10)
        self.cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **base},
                      session_id=page_session, timeout=10)

    def press_key(self, key: str, modifiers: int, target_id: str | None) -> dict[str, Any]:
        if key not in self.KEYS:
            raise BrowserError(
                "unknown_key", key, status=400, allowed=sorted(self.KEYS),
            )
        _tid, sid = self.session(target_id)
        self._press(sid, key, modifiers)
        return {"key": key, "modifiers": modifiers}


CONTROLLER = BrowserController()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class NavigateIn(BaseModel):
    url: str = Field(max_length=4096)
    target_id: str | None = Field(default=None, max_length=128)
    wait_until: Literal["load", "domcontentloaded", "networkidle", "none"] = "load"
    timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)


class SnapshotIn(BaseModel):
    target_id: str | None = Field(default=None, max_length=128)
    max_elements: int = Field(default=SNAPSHOT_MAX_ELEMENTS, ge=1, le=1000)
    include_text: bool = True
    max_text_nodes: int = Field(default=SNAPSHOT_MAX_TEXT_NODES, ge=0, le=400)
    name_filter: str | None = Field(default=None, max_length=200)
    role_filter: str | None = Field(default=None, max_length=64)
    viewport_only: bool = False
    format: Literal["lines", "json"] = "lines"


class RefIn(BaseModel):
    ref: str = Field(pattern=r"^e\d{1,9}$")
    snapshot_id: str | None = Field(default=None, pattern=r"^s\d{1,9}$")
    force: bool = False


class ClickIn(RefIn):
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=3)
    modifiers: int = Field(default=0, ge=0, le=15, description="1=alt 2=ctrl 4=meta 8=shift")


class TypeIn(RefIn):
    text: str = Field(max_length=20_000)
    clear: bool = True
    submit: bool = False
    mode: Literal["auto", "keys", "insert"] = "auto"
    delay_ms: int = Field(default=0, ge=0, le=200)


class SelectIn(RefIn):
    values: list[str] = Field(min_length=1, max_length=50)


class ScrollIn(BaseModel):
    ref: str | None = Field(default=None, pattern=r"^e\d{1,9}$")
    target_id: str | None = Field(default=None, max_length=128)
    direction: Literal["up", "down", "left", "right"] = "down"
    amount_px: int = Field(default=600, ge=1, le=20_000)


class KeyIn(BaseModel):
    key: str = Field(max_length=32)
    modifiers: int = Field(default=0, ge=0, le=15)
    target_id: str | None = Field(default=None, max_length=128)


class TextIn(BaseModel):
    target_id: str | None = Field(default=None, max_length=128)
    selector: str | None = Field(default=None, max_length=512)
    max_chars: int = Field(default=20_000, ge=100, le=200_000)


class ExtractField(BaseModel):
    name: str = Field(max_length=64)
    selector: str | None = Field(default=None, max_length=512)
    attr: str | None = Field(default=None, max_length=64)


class ExtractIn(BaseModel):
    target_id: str | None = Field(default=None, max_length=128)
    selector: str = Field(max_length=512)
    fields: list[ExtractField] = Field(default_factory=list, max_length=30)
    limit: int = Field(default=100, ge=1, le=1000)


class WaitIn(BaseModel):
    target_id: str | None = Field(default=None, max_length=128)
    until: Literal["load", "selector", "text", "gone"] = "selector"
    selector: str | None = Field(default=None, max_length=512)
    text: str | None = Field(default=None, max_length=500)
    state: Literal["visible", "attached", "hidden", "detached"] = "visible"
    timeout_ms: int = Field(default=10_000, ge=100, le=120_000)


class TabNewIn(BaseModel):
    url: str = Field(default="about:blank", max_length=4096)
    activate: bool = True


class TabIdIn(BaseModel):
    target_id: str = Field(max_length=128)


class HistoryIn(BaseModel):
    target_id: str | None = Field(default=None, max_length=128)


class ReloadIn(HistoryIn):
    ignore_cache: bool = False


class DialogIn(BaseModel):
    accept: bool
    prompt_text: str | None = Field(default=None, max_length=2000)
    target_id: str | None = Field(default=None, max_length=128)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
router = APIRouter(prefix="/browser", tags=["browser"])


def _acted(ref: str, entry: ElementRef, probe: dict[str, Any], started: float,
           detail: dict[str, Any]) -> dict[str, Any]:
    """Uniform envelope for every by-reference action."""
    out: dict[str, Any] = {
        "ok": True,
        "ref": ref,
        "role": entry.role,
        "name": entry.name,
        "target_id": entry.target_id,
        "took_ms": round((time.time() - started) * 1000, 1),
    }
    if probe.get("switched_to_tab"):
        out["switched_to_tab"] = probe["switched_to_tab"]
    out.update(detail)
    return out


def health_summary() -> dict[str, Any]:
    """Small, cheap block for /health. Never raises."""
    ok = CONTROLLER.cdp.reachable()
    out: dict[str, Any] = {
        "cdp_enabled": os.getenv("NESQ_BROWSER_ENABLED", "1") not in ("0", "false", "no"),
        "cdp_endpoint": f"{CDP_HOST}:{CDP_PORT}",
        "cdp_reachable": ok,
        "loopback_only": CDP_HOST in ("127.0.0.1", "localhost", "::1"),
    }
    if ok:
        out["browser"] = CONTROLLER.cdp.browser_version.get("Browser")
        # /json/list over plain HTTP, so the readiness probe never opens (or
        # revives) the debugger websocket.
        try:
            out["tabs"] = sum(
                1 for t in CONTROLLER.cdp.http_json("/json/list", timeout=2.0)
                if t.get("type") == "page"
            )
        except Exception:  # noqa: BLE001
            out["tabs"] = None
    return out


@router.get("/status")
def status() -> dict[str, Any]:
    if not CONTROLLER.cdp.reachable():
        raise _unavailable("no answer on the debugging port")
    pages = CONTROLLER.targets()
    active = CONTROLLER.active_target()
    return {
        "ok": True,
        "browser": CONTROLLER.cdp.browser_version.get("Browser"),
        "protocol": CONTROLLER.cdp.browser_version.get("Protocol-Version"),
        "endpoint": f"{CDP_HOST}:{CDP_PORT}",
        "active_target": active,
        "tabs": [
            {"target_id": p["targetId"], "url": p.get("url", ""),
             "title": _clip(p.get("title", ""), 120), "active": p["targetId"] == active}
            for p in pages
        ],
        "snapshots_live": sorted(CONTROLLER._snapshots),
        "pending_dialogs": list(CONTROLLER.cdp.dialogs.values()),
    }


@router.post("/navigate")
def navigate(body: NavigateIn) -> dict[str, Any]:
    return CONTROLLER.navigate(
        body.url, body.target_id, body.wait_until, body.timeout_ms / 1000
    )


@router.post("/snapshot")
def snapshot(body: SnapshotIn) -> dict[str, Any]:
    return CONTROLLER.snapshot(
        target_id=body.target_id,
        max_elements=body.max_elements,
        include_text=body.include_text,
        text_limit=body.max_text_nodes,
        name_filter=body.name_filter,
        role_filter=body.role_filter,
        viewport_only=body.viewport_only,
        fmt=body.format,
    )


@router.post("/click")
def click(body: ClickIn) -> dict[str, Any]:
    started = time.time()
    entry, probe = CONTROLLER.resolve(body.ref, body.snapshot_id, force=body.force)
    detail = CONTROLLER.click(
        entry, probe, button=body.button, click_count=body.click_count,
        modifiers=body.modifiers, force=body.force,
    )
    return _acted(body.ref, entry, probe, started, detail)


@router.post("/type")
def type_into(body: TypeIn) -> dict[str, Any]:
    started = time.time()
    entry, probe = CONTROLLER.resolve(body.ref, body.snapshot_id, force=body.force)
    detail = CONTROLLER.type_text(
        entry, body.text, clear=body.clear, mode=body.mode, delay_ms=body.delay_ms
    )
    if body.submit:
        CONTROLLER._press(CONTROLLER.cdp.session_for(entry.target_id), "Enter")
        detail["submitted"] = True
    return _acted(body.ref, entry, probe, started, detail)


@router.post("/select")
def select(body: SelectIn) -> dict[str, Any]:
    started = time.time()
    entry, probe = CONTROLLER.resolve(body.ref, body.snapshot_id, force=body.force)
    res = CONTROLLER._call_on(entry, JS_SELECT, [list(body.values)]) or {}
    if not res.get("ok"):
        raise BrowserError(
            "select_failed", res.get("reason", "unknown"), status=409,
            options=res.get("options"),
            hint=("this ref is not a native <select>; ARIA comboboxes are driven by "
                  "clicking the control and then the option"
                  if res.get("reason") == "not_a_select" else None),
        )
    return _acted(body.ref, entry, probe, started, {"selected": res.get("selected")})


@router.post("/hover")
def hover(body: RefIn) -> dict[str, Any]:
    started = time.time()
    entry, probe = CONTROLLER.resolve(body.ref, body.snapshot_id, force=body.force)
    cx, cy = CONTROLLER.point_for(entry, probe, force=body.force)
    CONTROLLER.cdp.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": cx, "y": cy, "button": "none", "buttons": 0},
        session_id=CONTROLLER.cdp.session_for(entry.target_id), timeout=10,
    )
    return _acted(body.ref, entry, probe, started, {"x": round(cx, 1), "y": round(cy, 1)})


@router.post("/scroll")
def scroll(body: ScrollIn) -> dict[str, Any]:
    dx = {"left": -body.amount_px, "right": body.amount_px}.get(body.direction, 0)
    dy = {"up": -body.amount_px, "down": body.amount_px}.get(body.direction, 0)
    if body.ref:
        entry, _probe = CONTROLLER.resolve(body.ref, None)
        CONTROLLER.cdp.send(
            "DOM.scrollIntoViewIfNeeded", {"backendNodeId": entry.backend_node_id},
            session_id=entry.session_id, timeout=10, allow_error=True,
        )
        return {"ok": True, "ref": body.ref, "scrolled_into_view": True}
    _tid, sid = CONTROLLER.session(body.target_id)
    return {"ok": True, **(CONTROLLER.call_global(sid, JS_SCROLL_WINDOW, [dx, dy]) or {})}


@router.post("/key")
def key(body: KeyIn) -> dict[str, Any]:
    return {"ok": True, **CONTROLLER.press_key(body.key, body.modifiers, body.target_id)}


@router.post("/text")
def text(body: TextIn) -> dict[str, Any]:
    _tid, sid = CONTROLLER.session(body.target_id)
    value = CONTROLLER.call_global(sid, JS_TEXT, [body.selector, body.max_chars]) or {}
    if not value.get("ok"):
        raise BrowserError("selector_not_found", body.selector or "", status=409)
    meta = CONTROLLER._page_meta(sid)
    return {"ok": True, "url": meta["url"], "title": meta["title"], **value}


@router.post("/extract")
def extract(body: ExtractIn) -> dict[str, Any]:
    _tid, sid = CONTROLLER.session(body.target_id)
    fields = [f.model_dump() for f in body.fields]
    value = CONTROLLER.call_global(
        sid, JS_EXTRACT, [body.selector, fields, body.limit]
    ) or {}
    if not value.get("ok"):
        raise BrowserError("bad_selector", value.get("detail", body.selector), status=400)
    return {"ok": True, "truncated": value["total"] > value["returned"], **value}


@router.post("/wait")
def wait(body: WaitIn) -> dict[str, Any]:
    _tid, sid = CONTROLLER.session(body.target_id)
    deadline = time.time() + body.timeout_ms / 1000
    if body.until == "selector" and not body.selector:
        raise BrowserError("missing_selector", "until=selector needs a selector")
    if body.until == "text" and not body.text:
        raise BrowserError("missing_text", "until=text needs text")

    # until=load polls document.readyState rather than waiting for the
    # Page lifecycle event: on an already-loaded page that event fired long ago
    # and waiting for it would always burn the full timeout.
    mode = {"load": "load", "text": "text"}.get(body.until, body.state)
    if body.until == "gone":
        mode = "detached" if body.state in ("detached", "attached") else "hidden"

    started = time.time()
    while True:
        value = CONTROLLER.call_global(sid, JS_WAIT, [body.selector, mode, body.text]) or {}
        if value.get("error"):
            raise BrowserError("bad_selector", value["error"], status=400)
        if value.get("done"):
            return {"ok": True, "until": body.until, "state": mode,
                    "waited_ms": round((time.time() - started) * 1000)}
        if time.time() >= deadline:
            raise BrowserError(
                "wait_timeout",
                f"{body.until}={body.selector or body.text!r} state={mode} "
                f"did not happen in {body.timeout_ms}ms",
                status=504,
            )
        time.sleep(0.15)


@router.get("/tabs")
def tabs() -> dict[str, Any]:
    pages = CONTROLLER.targets()
    active = CONTROLLER.active_target()
    return {
        "ok": True,
        "count": len(pages),
        "active_target": active,
        "tabs": [
            {"target_id": p["targetId"], "url": p.get("url", ""),
             "title": _clip(p.get("title", ""), 120), "active": p["targetId"] == active}
            for p in pages
        ],
    }


@router.post("/tabs/new")
def tab_new(body: TabNewIn) -> dict[str, Any]:
    if not ALLOWED_URL_RE.match(body.url):
        raise BrowserError("url_not_allowed", body.url)
    res = CONTROLLER.cdp.send("Target.createTarget", {"url": body.url})
    tid = res.get("targetId", "")
    if body.activate:
        CONTROLLER.active_target(tid)
        CONTROLLER.cdp.send("Target.activateTarget", {"targetId": tid}, allow_error=True)
    return {"ok": True, "target_id": tid, "url": body.url}


@router.post("/tabs/activate")
def tab_activate(body: TabIdIn) -> dict[str, Any]:
    tid = CONTROLLER.active_target(body.target_id)
    CONTROLLER.cdp.send("Target.activateTarget", {"targetId": tid}, allow_error=True)
    _sid = CONTROLLER.cdp.session_for(tid)
    return {"ok": True, "active_target": tid, **CONTROLLER._page_meta(_sid)}


@router.post("/tabs/close")
def tab_close(body: TabIdIn) -> dict[str, Any]:
    known = {p["targetId"] for p in CONTROLLER.targets()}
    if body.target_id not in known:
        raise BrowserError("unknown_target", body.target_id, status=409)
    CONTROLLER._invalidate(body.target_id)
    CONTROLLER.cdp.send("Target.closeTarget", {"targetId": body.target_id})
    if CONTROLLER._active_target == body.target_id:
        CONTROLLER._active_target = None
    return {"ok": True, "closed": body.target_id}


@router.post("/back")
def back(body: HistoryIn) -> dict[str, Any]:
    _tid, sid = CONTROLLER.session(body.target_id)
    return CONTROLLER.history(sid, -1)


@router.post("/forward")
def forward(body: HistoryIn) -> dict[str, Any]:
    _tid, sid = CONTROLLER.session(body.target_id)
    return CONTROLLER.history(sid, +1)


@router.post("/reload")
def reload(body: ReloadIn) -> dict[str, Any]:
    tid, sid = CONTROLLER.session(body.target_id)
    CONTROLLER._invalidate(tid)
    CONTROLLER.cdp.send("Page.reload", {"ignoreCache": body.ignore_cache},
                        session_id=sid, timeout=NAV_TIMEOUT)
    state = CONTROLLER._await_load(sid, "load", 20)
    return {**CONTROLLER._page_meta(sid), "load_state": state}


@router.post("/dialog")
def dialog(body: DialogIn) -> dict[str, Any]:
    _tid, sid = CONTROLLER.session(body.target_id)
    pending = CONTROLLER.cdp.dialogs.get(sid)
    if not pending:
        raise BrowserError("no_dialog", "no javascript dialog is open on this tab", status=409)
    params: dict[str, Any] = {"accept": body.accept}
    if body.prompt_text is not None:
        params["promptText"] = body.prompt_text
    CONTROLLER.cdp.send("Page.handleJavaScriptDialog", params, session_id=sid, timeout=10)
    CONTROLLER.cdp.dialogs.pop(sid, None)
    return {"ok": True, "handled": pending}


# --------------------------------------------------------------------------- #
# Why no /browser/eval
# --------------------------------------------------------------------------- #
# A generic "run this JavaScript" endpoint would be two lines of code and would
# make every one of the endpoints above redundant. It is deliberately absent.
#
# This browser holds the bot's persistent logins. An endpoint that runs
# caller-supplied JS turns any leak of the sidecar token - or any SSRF that can
# reach :7910 - into arbitrary code execution inside those authenticated
# sessions: read every cookie the page can see, exfiltrate the DOM of an
# internal app, issue same-origin POSTs as the user. Nothing else in this
# sidecar has that blast radius, and no amount of allow-listing at the API
# layer can constrain a string that Chrome will happily execute.
#
# The escape hatch that is actually needed in practice - "reach into the page
# for something the AX tree does not expose" - is served by /browser/extract
# and /browser/text, which take CSS *selectors*. A selector is data: it can
# only read, it cannot call, assign or navigate. If a future page needs
# something those cannot express, add a named, fixed function here rather than
# opening the general door.
