"""Agent sidecar - the control plane for one Bot Desktop.

Exposes mouse / keyboard / screenshot / clipboard over HTTP so the API (and,
through it, a bot) can drive the X session inside this container.

Two ways to drive it
--------------------
* The *pixel* API - ``/screenshot`` + ``/action`` - is the original surface and
  is unchanged. It is the fallback for canvas, CAPTCHAs, PDF viewers and every
  non-browser app on the desktop.
* The *CDP* API - ``/browser/*``, implemented in ``browser.py`` - drives the
  same Chromium through the DevTools Protocol, so a web page arrives as a few
  KB of ``ref role "name"`` lines instead of a screenshot. Use it for anything
  inside the browser.

Both share the ``X-Nesq-Sidecar-Token`` gate.

Security model
--------------
Every endpoint except ``/health`` requires the shared secret
``X-Nesq-Sidecar-Token``. The token is read from ``NESQ_SIDECAR_TOKEN``. When
it is unset the sidecar still works - a laptop should not need ceremony - but
it logs a loud warning on boot and on every unauthenticated call, because on a
pod network an open control plane means anything in the namespace can type
into this desktop.

``/health`` stays open so kubelet probes and the Docker HEALTHCHECK do not need
the secret. It deliberately reports capability, never content.

Contract note: ``/screenshot`` keeps returning ``png_base64`` for the default
PNG format, because docs/API.md pins that key. JPEG responses add
``image_base64`` + ``mime`` instead.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s level=%(levelname)s logger=sidecar %(message)s",
)
logger = logging.getLogger("nesq.sidecar")

VERSION = "0.3.0"

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
TOKEN = os.getenv("NESQ_SIDECAR_TOKEN", "").strip()
TOKEN_HEADER = "X-Nesq-Sidecar-Token"
PORT = int(os.getenv("NESQ_SIDECAR_PORT", "7910"))
BIND = os.getenv("NESQ_SIDECAR_BIND", "0.0.0.0")
DISPLAY = os.getenv("DISPLAY", ":1")
BOT_SLUG = os.getenv("BOT_SLUG", "bot")

# Screenshots used to go to /tmp/nesq-shot.png: a fixed, world-readable,
# predictable path that anything else in the container could read or
# pre-create as a symlink. This directory is created 0700, and _ensure_workdir
# refuses to use it if it turns out to belong to someone else.
WORKDIR = os.getenv("NESQ_SIDECAR_WORKDIR", "/tmp/nesq-sidecar")  # noqa: S108

# Hard ceiling on how long any X tool may block. Without it a modal dialog can
# wedge a request thread forever.
CMD_TIMEOUT = float(os.getenv("NESQ_SIDECAR_CMD_TIMEOUT", "15"))

REQUIRED_TOOLS = ("xdotool", "scrot", "wmctrl", "xclip", "xdpyinfo")
OPTIONAL_TOOLS = ("chromium", "x11vnc", "websockify")

_START_TIME = time.time()


def _ensure_workdir() -> None:
    """Create the scratch directory 0700, refusing anything we do not own.

    `makedirs(exist_ok=True)` alone would happily reuse a directory - or a
    symlink to one - planted by another user, which is the exact hole the old
    fixed /tmp path had.
    """
    os.makedirs(WORKDIR, mode=0o700, exist_ok=True)
    info = os.lstat(WORKDIR)
    if not os.path.isdir(WORKDIR) or os.path.islink(WORKDIR):
        raise RuntimeError(f"{WORKDIR} is not a real directory")
    if info.st_uid != os.getuid():
        raise RuntimeError(
            f"{WORKDIR} is owned by uid {info.st_uid}, not {os.getuid()} - refusing to use it"
        )
    try:
        os.chmod(WORKDIR, 0o700)
    except OSError:  # pragma: no cover
        logger.warning("could not tighten permissions on %s", WORKDIR)


_ensure_workdir()

if not TOKEN:
    logger.warning(
        "########################################################################"
    )
    logger.warning("# NESQ_SIDECAR_TOKEN is not set.")
    logger.warning(
        "# This sidecar will accept UNAUTHENTICATED clicks, keystrokes and"
    )
    logger.warning(
        "# screenshots from anything that can reach %s:%s.", BIND, PORT
    )
    logger.warning("# Acceptable for local development. Never in a cluster.")
    logger.warning(
        "########################################################################"
    )
else:
    logger.info("sidecar auth enabled header=%s", TOKEN_HEADER)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
_warned_open_calls = 0


def require_token(
    request: Request,
    x_nesq_sidecar_token: str | None = Header(default=None),
) -> None:
    """Constant-time shared-secret check. No-op (with a warning) when unset."""
    global _warned_open_calls

    if not TOKEN:
        _warned_open_calls += 1
        # Log the first few and then every 100th, so a busy dev loop does not
        # drown the log while the warning still stays visible.
        if _warned_open_calls <= 3 or _warned_open_calls % 100 == 0:
            logger.warning(
                "unauthenticated %s %s from %s (NESQ_SIDECAR_TOKEN unset, call #%d)",
                request.method,
                request.url.path,
                request.client.host if request.client else "?",
                _warned_open_calls,
            )
        return

    if not x_nesq_sidecar_token or not secrets.compare_digest(
        x_nesq_sidecar_token, TOKEN
    ):
        logger.warning(
            "rejected %s %s from %s: bad or missing %s",
            request.method,
            request.url.path,
            request.client.host if request.client else "?",
            TOKEN_HEADER,
        )
        raise HTTPException(
            status_code=401,
            detail="invalid_sidecar_token",
            headers={"WWW-Authenticate": TOKEN_HEADER},
        )


app = FastAPI(
    title="Nesq Bot Desktop Sidecar",
    version=VERSION,
    docs_url=None if TOKEN else "/docs",
    redoc_url=None,
)


# --------------------------------------------------------------------------- #
# CDP browser control (/browser/*)
# --------------------------------------------------------------------------- #
# Imported defensively: a missing websocket-client or a CDP bug must degrade
# this sidecar to the pixel API, never stop it from booting. Three clients
# depend on /action and /screenshot.
try:
    import browser as _browser

    app.include_router(_browser.router, dependencies=[Depends(require_token)])
    app.add_exception_handler(_browser.BrowserError, _browser.browser_error_handler)
    BROWSER_IMPORT_ERROR: str | None = None
    logger.info("cdp browser api enabled endpoint=%s:%s", _browser.CDP_HOST, _browser.CDP_PORT)
except Exception as _exc:  # noqa: BLE001
    _browser = None  # type: ignore[assignment]
    BROWSER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    logger.error(
        "cdp browser api DISABLED (%s) - the pixel API still works", BROWSER_IMPORT_ERROR
    )


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #
class ToolError(RuntimeError):
    pass


def run(cmd: list[str], *, timeout: float | None = None, input_bytes: bytes | None = None) -> str:
    """Run an X tool. Never uses a shell, so arguments cannot be injected."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            input=input_bytes,
            capture_output=True,
            timeout=timeout or CMD_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{cmd[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{cmd[0]} timed out after {timeout or CMD_TIMEOUT}s") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise ToolError(f"{cmd[0]} failed rc={proc.returncode}: {detail[:400]}")
    return (proc.stdout or b"").decode("utf-8", "replace")


# xdotool keysyms: letters, digits, underscore, plus for chords.
_KEY_RE = re.compile(r"^[A-Za-z0-9_+]{1,64}$")

#: Browser key names -> X11 keysyms.
#:
#: This exists because a person taking over a desktop could not press Tab, Enter
#: or Backspace — the three keys a sign-in form is made of. The clients send
#: JavaScript `KeyboardEvent.key` values, lowercased; X11 keysyms are
#: case-sensitive and spelled differently (`Return`, not `enter`; `BackSpace`,
#: not `backspace`). `_validate_key` only ever checked the *shape* of the string,
#: so those names passed validation, reached `xdotool`, matched no keysym, and
#: did nothing at all. No error surfaced anywhere: the keystroke was simply
#: swallowed, which is the worst possible failure for someone typing a password
#: they cannot see the effect of.
#:
#: Mapping here rather than in the desktop app because three clients send these
#: — desktop, mobile and the agent loop — and a translation table that lives in
#: one of them is a table the other two do not have.
_KEYSYM_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "escape": "Escape",
    "esc": "Escape",
    "space": "space",
    " ": "space",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "pageup": "Prior",
    "pagedown": "Next",
    "home": "Home",
    "end": "End",
    "insert": "Insert",
    "capslock": "Caps_Lock",
    "control": "ctrl",
    "ctrl": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "meta": "super",
    "super": "super",
    "os": "super",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}


def _canonical_key(key: str) -> str:
    """Translate one key name to an X11 keysym, or leave it alone.

    Unknown names pass through unchanged: a caller that already knows the keysym
    it wants (`Return`, `KP_Enter`, `a`) must keep working, and inventing a
    rejection here would break the agent loop, which has always sent real
    keysyms.
    """
    return _KEYSYM_ALIASES.get(key.strip().lower(), key)


def _validate_key(key: str) -> str:
    resolved = "+".join(_canonical_key(part) for part in key.split("+")) if "+" in key else _canonical_key(key)
    if not _KEY_RE.match(resolved):
        raise ValueError(f"invalid key name: {key!r}")
    return resolved


def _screen_size() -> tuple[int, int] | None:
    try:
        out = run(["xdpyinfo", "-display", DISPLAY], timeout=5)
    except Exception:  # noqa: BLE001
        return None
    match = re.search(r"dimensions:\s+(\d+)x(\d+)", out)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
# STABLE CONTRACT: `click`, `type`, `key` (with a `keys` array) and `mousemove`
# are what the desktop app, the mobile app and the orchestrator all send. They
# keep their exact names and payload shape. Everything after them is additive.
ActionName = Literal[
    "click",
    "double_click",
    "right_click",
    "middle_click",
    "mousedown",
    "mouseup",
    "mousemove",
    "drag",
    "scroll",
    "type",
    "key",
    "key_combo",
    "clipboard_set",
    "open_chromium",
    "focus_window",
    "close_window",
]


class ActionIn(BaseModel):
    action: ActionName

    # pointer
    x: int | None = Field(default=None, ge=-1, le=32767)
    y: int | None = Field(default=None, ge=-1, le=32767)
    to_x: int | None = Field(default=None, ge=-1, le=32767, description="drag target")
    to_y: int | None = Field(default=None, ge=-1, le=32767, description="drag target")
    # Numeric xdotool button, as a string. The API normalises left/right/middle
    # to 1/2/3 before the request gets here, so there is deliberately no second
    # normalisation in this file - two of them would eventually disagree.
    button: str = Field(default="1", pattern=r"^[1-9]$")
    steps: int = Field(default=25, ge=1, le=500, description="drag interpolation steps")
    hold_ms: int = Field(default=60, ge=0, le=5000)

    # scrolling
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(default=3, ge=1, le=50, description="scroll clicks")

    # text / keys
    text: str | None = Field(default=None, max_length=100_000)
    delay_ms: int = Field(default=12, ge=0, le=1000, description="per-keystroke delay")
    keys: list[str] = Field(default_factory=list, max_length=32)
    combo: str | None = Field(default=None, max_length=64, description="e.g. ctrl+shift+t")
    repeat: int = Field(default=1, ge=1, le=20)

    # windows
    window: str | None = Field(default=None, max_length=256)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> JSONResponse:
    """Capability report. Open on purpose so probes work without the secret.

    ``ok`` is true only when X is reachable AND every required tool exists -
    the entrypoint's readiness gate and the container HEALTHCHECK both key off
    this, so it must not go green on a half-built desktop.
    """
    tools = {name: bool(shutil.which(name)) for name in REQUIRED_TOOLS}
    optional = {name: bool(shutil.which(name)) for name in OPTIONAL_TOOLS}
    size = _screen_size()
    display_ok = size is not None
    missing = [name for name, present in tools.items() if not present]

    body: dict[str, Any] = {
        "ok": display_ok and not missing,
        "service": "nesq-bot-desktop-sidecar",
        "version": VERSION,
        "bot": BOT_SLUG,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "display": {
            "name": DISPLAY,
            "reachable": display_ok,
            "width": size[0] if size else None,
            "height": size[1] if size else None,
        },
        "tools": tools,
        "optional_tools": optional,
        "missing_tools": missing,
        "auth": {"required": bool(TOKEN), "header": TOKEN_HEADER},
        "workdir": WORKDIR,
    }

    # Browser control is reported but deliberately does NOT gate `ok`: a bot
    # with a wedged Chromium can still be driven by pixels, and the readiness
    # probe should not restart the whole desktop over it.
    if _browser is not None:
        try:
            body["browser"] = _browser.health_summary()
        except Exception as exc:  # noqa: BLE001
            body["browser"] = {"cdp_reachable": False, "error": str(exc)}
    else:
        body["browser"] = {"cdp_reachable": False, "error": BROWSER_IMPORT_ERROR}

    # 200 either way: an unhealthy-but-answering sidecar is more useful to
    # debug than a connection error. Callers check `ok`. The container
    # HEALTHCHECK only needs the port to answer; the entrypoint logs `ok`.
    return JSONResponse(body)


# --------------------------------------------------------------------------- #
# Screenshot
# --------------------------------------------------------------------------- #
#: PNG magic. A file that does not start with this is not worth handing to PIL.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Captures to attempt before giving up. One retry, not a loop: a display that
#: cannot be photographed twice in a row has something wrong with it that
#: photographing a third time will not fix.
_CAPTURE_ATTEMPTS = 2


def _capture_display() -> tuple[Any, str]:
    """Photograph the X display, or say why not.

    Written this way because a person finished a takeover, handed the screen
    back, and the agent stopped with `decode failed: broken PNG file (chunk
    b'\\x00\\x00\\x00\\x00')` — a run abandoned at the exact moment it was being
    given back its session. Three separate faults produced that one message:

    * **A fixed filename.** `shot-{pid}.png` is reused for every capture, so a
      previous interrupted `scrot` leaves a truncated file behind and the next
      request happily decodes the corpse.
    * **Exit code trusted over output.** `scrot` can exit 0 and still write a
      short or empty file, which is what a display being resized under it — a
      browser going fullscreen, a session handed back — tends to produce.
    * **No retry.** A transient capture glitch became a terminal error for the
      whole run.

    So: a unique path per attempt, the bytes checked before PIL sees them, and
    one retry. The image is decoded from memory, so the file is gone before it
    can be mistaken for the next one.
    """
    last = "screenshot failed"
    for attempt in range(1, _CAPTURE_ATTEMPTS + 1):
        path = os.path.join(WORKDIR, f"shot-{os.getpid()}-{uuid.uuid4().hex}.png")
        try:
            try:
                run(["scrot", "--overwrite", path])
            except ToolError as exc:
                last = str(exc)
                continue

            try:
                blob = Path(path).read_bytes()
            except OSError as exc:
                last = f"scrot wrote no file: {exc}"
                continue

            # Check the bytes before PIL does, so the error names the real
            # problem instead of whichever chunk the decoder tripped over.
            if len(blob) < len(_PNG_MAGIC) or not blob.startswith(_PNG_MAGIC):
                last = (
                    f"scrot produced {len(blob)} bytes that are not a PNG - the "
                    "display was probably mid-resize"
                )
                continue

            try:
                with Image.open(io.BytesIO(blob)) as raw:
                    return raw.convert("RGB"), ""
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last = f"decode failed: {exc}"
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        logger.warning("screenshot attempt %d/%d failed: %s", attempt, _CAPTURE_ATTEMPTS, last)
    return None, last


@app.get("/screenshot", dependencies=[Depends(require_token)])
def screenshot(
    fmt: Literal["png", "jpeg"] = Query("png", alias="format"),
    quality: int = Query(75, ge=1, le=95, description="JPEG quality"),
    x: int | None = Query(None, ge=0, description="crop origin"),
    y: int | None = Query(None, ge=0, description="crop origin"),
    w: int | None = Query(None, ge=1, description="crop width"),
    h: int | None = Query(None, ge=1, description="crop height"),
    max_width: int | None = Query(None, ge=64, le=7680, description="downscale cap"),
    grayscale: bool = Query(False),
) -> dict[str, Any]:
    """Capture the X display.

    A full 1440x900 PNG is ~1.5 MB base64, which is a painful payload to shove
    through the API into a model context on every step. Region cropping plus
    JPEG at q75 typically cuts that by 10-20x.
    """
    img, capture_error = _capture_display()
    if img is None:
        return {"ok": False, "error": capture_error or "screenshot failed"}

    full_w, full_h = img.width, img.height
    region = None

    if any(v is not None for v in (x, y, w, h)):
        left = x or 0
        top = y or 0
        right = min(full_w, left + w) if w else full_w
        bottom = min(full_h, top + h) if h else full_h
        if left >= right or top >= bottom:
            return {
                "ok": False,
                "error": f"empty crop region for a {full_w}x{full_h} screen",
            }
        img = img.crop((left, top, right, bottom))
        region = {"x": left, "y": top, "w": right - left, "h": bottom - top}

    scale = 1.0
    if max_width and img.width > max_width:
        scale = max_width / img.width
        img = img.resize(
            (max_width, max(1, round(img.height * scale))), Image.LANCZOS
        )

    if grayscale:
        img = img.convert("L")

    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        mime = "image/jpeg"
    else:
        img.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    payload = base64.b64encode(buf.getvalue()).decode("ascii")

    result: dict[str, Any] = {
        "ok": True,
        "width": img.width,
        "height": img.height,
        "screen_width": full_w,
        "screen_height": full_h,
        "region": region,
        "scale": round(scale, 4),
        "mime": mime,
        "bytes": buf.getbuffer().nbytes,
        "image_base64": payload,
    }
    if fmt == "png":
        # docs/API.md pins this key for the default response shape.
        result["png_base64"] = payload
    return result


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
@app.get("/windows", dependencies=[Depends(require_token)])
def windows() -> dict[str, Any]:
    try:
        out = run(["wmctrl", "-l", "-G", "-p"])
    except ToolError as exc:
        return {"ok": False, "error": str(exc), "windows": []}

    parsed: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # id desktop pid x y w h host title
        parts = line.split(None, 8)
        if len(parts) < 9:
            parsed.append({"raw": line})
            continue
        wid, desk, pid, wx, wy, ww, wh, host, title = parts
        try:
            parsed.append(
                {
                    "id": wid,
                    "desktop": int(desk),
                    "pid": int(pid),
                    "x": int(wx),
                    "y": int(wy),
                    "width": int(ww),
                    "height": int(wh),
                    "host": host,
                    "title": title,
                }
            )
        except ValueError:
            parsed.append({"raw": line})

    return {"ok": True, "count": len(parsed), "windows": parsed}


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #
@app.get("/clipboard_get", dependencies=[Depends(require_token)])
def clipboard_get(
    selection: Literal["clipboard", "primary", "secondary"] = Query("clipboard"),
    max_chars: int = Query(100_000, ge=1, le=1_000_000),
) -> dict[str, Any]:
    """Read an X selection. Closes the copy/paste loop for form filling."""
    try:
        text = run(["xclip", "-selection", selection, "-o"])
    except ToolError as exc:
        message = str(exc)
        # An empty selection makes xclip exit non-zero; that is not an error.
        if "Error: target STRING not available" in message:
            return {"ok": True, "selection": selection, "text": "", "truncated": False}
        return {"ok": False, "error": message}

    truncated = len(text) > max_chars
    return {
        "ok": True,
        "selection": selection,
        "text": text[:max_chars],
        "length": len(text),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def _require_point(body: ActionIn) -> tuple[int, int]:
    if body.x is None or body.y is None:
        raise ValueError(f"{body.action} requires x and y")
    return body.x, body.y


_SCROLL_BUTTON = {"up": "4", "down": "5", "left": "6", "right": "7"}


@app.post("/action", dependencies=[Depends(require_token)])
def action(body: ActionIn) -> dict[str, Any]:
    started = time.time()
    try:
        detail = _dispatch(body)
    except ValueError as exc:
        return {"ok": False, "action": body.action, "error": str(exc)}
    except ToolError as exc:
        logger.warning("action=%s failed: %s", body.action, exc)
        return {"ok": False, "action": body.action, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("action=%s crashed", body.action)
        return {"ok": False, "action": body.action, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "action": body.action,
        "took_ms": round((time.time() - started) * 1000, 1),
        **(detail or {}),
    }


def _dispatch(body: ActionIn) -> dict[str, Any] | None:  # noqa: C901 - flat command table
    a = body.action

    if a in ("click", "double_click", "right_click", "middle_click"):
        button = {"right_click": "3", "middle_click": "2"}.get(a, body.button)
        cmd = ["xdotool"]
        if body.x is not None and body.y is not None:
            cmd += ["mousemove", str(body.x), str(body.y)]
        clicks = 2 if a == "double_click" else 1
        cmd += ["click", "--repeat", str(clicks), "--delay", "80", button]
        run(cmd)
        return {"button": button, "clicks": clicks}

    if a == "mousemove":
        x, y = _require_point(body)
        run(["xdotool", "mousemove", str(x), str(y)])
        return {"x": x, "y": y}

    if a in ("mousedown", "mouseup"):
        cmd = ["xdotool"]
        if body.x is not None and body.y is not None:
            cmd += ["mousemove", str(body.x), str(body.y)]
        cmd += [a, body.button]
        run(cmd)
        return {"button": body.button}

    if a == "drag":
        # Press, move in interpolated steps, release. One xdotool invocation so
        # the button state cannot leak if the request dies mid-way.
        x, y = _require_point(body)
        if body.to_x is None or body.to_y is None:
            raise ValueError("drag requires to_x and to_y")
        cmd = [
            "xdotool",
            "mousemove", str(x), str(y),
            "mousedown", body.button,
        ]
        for i in range(1, body.steps + 1):
            ix = round(x + (body.to_x - x) * i / body.steps)
            iy = round(y + (body.to_y - y) * i / body.steps)
            cmd += ["mousemove", str(ix), str(iy)]
            if body.hold_ms:
                cmd += ["sleep", f"{body.hold_ms / 1000 / body.steps:.4f}"]
        cmd += ["mouseup", body.button]
        run(cmd, timeout=max(CMD_TIMEOUT, body.hold_ms / 1000 + 10))
        return {"from": [x, y], "to": [body.to_x, body.to_y], "steps": body.steps}

    if a == "scroll":
        button = _SCROLL_BUTTON[body.direction]
        cmd = ["xdotool"]
        if body.x is not None and body.y is not None:
            cmd += ["mousemove", str(body.x), str(body.y)]
        cmd += ["click", "--repeat", str(body.amount), "--delay", "40", button]
        run(cmd, timeout=max(CMD_TIMEOUT, body.amount * 0.1 + 5))
        return {"direction": body.direction, "amount": body.amount}

    if a == "type":
        if body.text is None:
            raise ValueError("type requires text")
        # `--` stops xdotool parsing text that begins with a dash as a flag.
        run(
            ["xdotool", "type", "--delay", str(body.delay_ms), "--", body.text],
            timeout=max(CMD_TIMEOUT, len(body.text) * body.delay_ms / 1000 + 10),
        )
        return {"chars": len(body.text)}

    if a == "key":
        keys = [_validate_key(k) for k in body.keys]
        if not keys:
            raise ValueError("key requires a non-empty keys list")
        run(["xdotool", "key", "--delay", str(body.delay_ms), *keys])
        return {"keys": keys}

    if a == "key_combo":
        # Chord: all modifiers held, one keystroke. `combo` wins; otherwise the
        # keys list is joined, so {"keys":["ctrl","shift","t"]} -> ctrl+shift+t.
        combo = body.combo or "+".join(body.keys)
        if not combo:
            raise ValueError("key_combo requires combo or keys")
        # Keep the translated form: `_validate_key` returns the X11 keysym and
        # discarding it meant `ctrl+enter` was validated and then sent verbatim.
        combo = _validate_key(combo)
        cmd = ["xdotool", "key"]
        if body.repeat > 1:
            cmd += ["--repeat", str(body.repeat), "--repeat-delay", "60"]
        cmd += ["--delay", str(body.delay_ms), combo]
        run(cmd)
        return {"combo": combo, "repeat": body.repeat}

    if a == "clipboard_set":
        run(
            ["xclip", "-selection", "clipboard", "-in"],
            input_bytes=(body.text or "").encode("utf-8"),
        )
        return {"chars": len(body.text or "")}

    if a == "open_chromium":
        if not shutil.which("chromium"):
            raise ToolError("chromium is not installed in this image")
        url = body.text or "about:blank"
        if not re.match(r"^(https?://|about:|file:///home/nesq/)", url):
            raise ValueError("open_chromium accepts http(s), about: or file:///home/nesq/ URLs")
        # Detached: the browser outlives this request by design.
        subprocess.Popen(  # noqa: S603
            [
                "chromium",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--user-data-dir={os.path.expanduser('~/.config/chromium-nesq')}",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"url": url}

    if a in ("focus_window", "close_window"):
        if not body.window:
            raise ValueError(f"{a} requires window (a wmctrl id or title substring)")
        flag = "-a" if a == "focus_window" else "-c"
        run(["wmctrl", flag, body.window])
        return {"window": body.window}

    raise ValueError(f"unknown action {a}")


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=BIND,
        port=PORT,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        access_log=False,
    )
