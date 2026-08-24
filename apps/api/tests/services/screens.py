"""Screenshot doubles shared by the agent-loop and desktop-agency suites.

Both suites need the same two things and used to grow their own copies of the
first one:

* a mock desktop whose frames actually *differ* between calls, so the loop's
  byte-identical-screen detector does not stop a scripted twenty-step run at
  step four;
* a way to make the mock behave like a real downscaling sidecar, because the
  coordinate mapping that downscaling forces is the risky half of the
  screenshot-size fix and it must be exercised end to end, not only in a unit
  test of the arithmetic.

The dimensions the mock reports are never touched here. They are the model's
coordinate space, `ScreenGeometry` is derived from them, and a fixture that
quietly changed the height by one pixel per call would put a 1.005 rescale
under every test in both files.
"""

from __future__ import annotations

import base64
import struct

from app.services import simulation
from app.services.desktop import _png_chunk, make_placeholder_png

#: The desktop a real deployment drives, and the size every published
#: image-token figure in this suite is quoted against.
REAL_SCREEN = (1280, 800)

#: Length of a PNG's IEND chunk: 4 length + 4 type + 0 data + 4 CRC.
_IEND_BYTES = 12


def distinct_png(width: int, height: int, nonce: int) -> bytes:
    """A valid PNG of exactly `width`x`height` whose bytes differ per `nonce`.

    The nonce rides in a `tEXt` chunk spliced in ahead of `IEND` rather than in
    the pixels, so the image the loop hashes is different on every call while
    its declared size — and therefore its coordinate space and its image-token
    price — stays put.
    """
    raw = make_placeholder_png(width, height)
    marker = _png_chunk(b"tEXt", b"nesq-test-nonce\x00" + str(nonce).encode("ascii"))
    return raw[:-_IEND_BYTES] + marker + raw[-_IEND_BYTES:]


def header_png(width: int, height: int, nonce: int) -> bytes:
    """PNG bytes whose IHDR declares `width`x`height`, different per `nonce`.

    Deliberately not a decodable image, and it does not need to be: in mock
    mode nothing decodes a screenshot. The two things that read these bytes are
    `model_router.png_dimensions`, which parses IHDR to price the frame, and
    the loop's SHA-256 change detector, which only needs them to differ.

    The alternative — a real `make_placeholder_png(1280, 800)` — is a million
    pure-Python pixel writes per frame, and a thirty-five step run needs
    thirty-six of them. The cost measurements this exists for would take longer
    than the rest of the suite put together.
    """
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"tEXt", b"nesq-test-nonce\x00" + str(nonce).encode("ascii"))
        + _png_chunk(b"IEND", b"")
    )


def patch_real_sized_screens(
    monkeypatch, *, screen: tuple[int, int] = REAL_SCREEN, max_width: int | None = None
) -> dict[str, int]:
    """A mock desktop the size of a real one, downscaling like the sidecar.

    `MOCK_SCREENSHOT_SIZE` is 320x200, which prices at one image tile and hides
    the whole cost question. Every token and dollar figure asserted in
    `test_agent_cost.py` is quoted against a 1280x800 desktop, so it captures
    at one.

    `max_width` overrides whatever the caller asked for; leave it None to let
    the loop's own `AGENT_SCREENSHOT_OPTIONS` decide, which is what makes this
    a measurement of the shipped configuration rather than of the test's.
    """
    counter = {"n": 0}
    screen_width, screen_height = screen

    async def _screenshot(db, bot_id, **options):
        counter["n"] += 1
        cap = max_width if max_width is not None else options.get("max_width")
        width, height, scale = screen_width, screen_height, 1.0
        if cap and int(cap) < screen_width:
            scale = int(cap) / screen_width
            width = int(cap)
            height = max(1, round(screen_height * scale))
        payload = base64.b64encode(header_png(width, height, counter["n"])).decode("ascii")
        return {
            "ok": True,
            "mock": True,
            "width": width,
            "height": height,
            "screen_width": screen_width,
            "screen_height": screen_height,
            "region": None,
            "scale": round(scale, 4),
            # PNG whatever was asked for, exactly like the real mock: encoding
            # a JPEG needs an image library the API does not ship, and a `mime`
            # that does not match the bytes would be a lie.
            "mime": "image/png",
            "image_base64": payload,
            "png_base64": payload,
        }

    monkeypatch.setattr(simulation._desktop, "screenshot", _screenshot)
    return counter


def patch_varying_screens(monkeypatch, *, max_width: int | None = None) -> dict[str, int]:
    """Point the mock desktop at `distinct_png`. Returns the call counter.

    `max_width` forces a downscale regardless of what the caller asked for,
    which is how a test puts the whole loop into a scaled coordinate space: the
    mock generates at the reduced size and reports `screen_width` /
    `screen_height` for the desktop behind it, exactly as the sidecar does.
    """
    counter = {"n": 0}
    real = simulation._desktop.screenshot

    async def _screenshot(db, bot_id, **options):
        if max_width is not None:
            options["max_width"] = max_width
        result = dict(await real(db, bot_id, **options))
        counter["n"] += 1
        if not result.get("ok"):
            return result
        png = distinct_png(int(result["width"]), int(result["height"]), counter["n"])
        payload = base64.b64encode(png).decode("ascii")
        for key in ("image_base64", "png_base64"):
            if key in result:
                result[key] = payload
        return result

    monkeypatch.setattr(simulation._desktop, "screenshot", _screenshot)
    return counter
