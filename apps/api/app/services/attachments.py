"""Message attachments: validation, storage shape, and how they reach a model.

A person can send files with a message. Two kinds are accepted:

* **images** — forwarded to the model as vision input (`image_url` content
  parts, the same shape `model_router.image_content_part` already builds for
  desktop screenshots), so "what does this chart say?" works without a
  desktop or a connector;
* **text-like files** — `.txt`, `.md`, `.csv`, JSON — inlined into the prompt
  under a heading naming the file. A bot reading a pasted CSV is the most
  common "attach a file" request there is, and it needs no vision model.

Everything else is refused with a 400 naming the type. There is deliberately
no PDF/Office parsing here: that is a document pipeline with its own failure
modes, and pretending a PDF was "attached" when only its filename reached the
model is worse than saying no.

Storage is `messages.meta["attachments"]`, one entry per file, base64 in
`data`. That is the pragmatic choice for the sizes allowed here (a few MB per
message, capped below): no blob store to provision, and the transcript row
is the natural owner. `public_meta` strips `data` before anything is sent to
a client — a transcript listing must not ship every image ever attached.
Bytes are fetched one at a time from
`GET /threads/{thread_id}/messages/{message_id}/attachments/{index}`.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from app.errors import AppError

# --------------------------------------------------------------------- limits

#: Files per message. Four images is already ~4.5k prompt tokens at `high`.
MAX_ATTACHMENTS = 4
#: Decoded bytes. Kept below what a JSONB row comfortably holds.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
#: Text inlined into the prompt per file, in characters. A 256 KB CSV is
#: ~65k tokens; the model gets the head and is told it was cut.
MAX_INLINE_CHARS = 24_000

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
TEXT_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
        "text/x-log",
    }
)
ALLOWED_TYPES = IMAGE_TYPES | TEXT_TYPES

#: How many of the most recent user messages keep their images when history
#: is replayed to the model. Older ones degrade to a text placeholder — an
#: image the person asked about five turns ago is not worth 1.1k tokens on
#: every subsequent turn.
HISTORY_IMAGE_MESSAGES = 2


# ------------------------------------------------------------------ validate


def normalise_media_type(value: str) -> str:
    """`image/jpg` → `image/jpeg`, parameters dropped, case folded."""
    media = (value or "").split(";", 1)[0].strip().lower()
    if media == "image/jpg":
        return "image/jpeg"
    if media == "application/x-ndjson":
        return "application/json"
    return media


def _field(item: Any, name: str) -> Any:
    """Pydantic model or plain dict — both arrive here."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def validate_attachments(items: list[Any] | None) -> list[dict[str, Any]]:
    """Check and normalise what a client sent.

    Returns the storage shape: `[{name, media_type, size, data}]` with `data`
    still base64. Raises `AppError(400, …)` with a code a client can act on:
    `too_many_attachments`, `attachment_type_unsupported`,
    `attachment_too_large`, `attachment_invalid`.
    """
    if not items:
        return []
    if len(items) > MAX_ATTACHMENTS:
        raise AppError(
            400,
            "too_many_attachments",
            f"At most {MAX_ATTACHMENTS} attachments per message",
        )
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        name = str(_field(item, "name") or "")
        media = normalise_media_type(str(_field(item, "media_type") or ""))
        raw = str(_field(item, "data") or "")
        name = name.strip() or f"attachment-{index + 1}"
        if media not in ALLOWED_TYPES:
            raise AppError(
                400,
                "attachment_type_unsupported",
                f"{name}: {media or 'unknown type'} is not accepted. "
                f"Images ({', '.join(sorted(IMAGE_TYPES))}) and text files "
                f"({', '.join(sorted(TEXT_TYPES))}) are.",
            )
        # Tolerate a data-URL prefix — a browser's FileReader produces one and
        # it is the easiest thing for a client to forget to strip.
        if raw.startswith("data:") and ";base64," in raw:
            raw = raw.split(";base64,", 1)[1]
        raw = "".join(raw.split())
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AppError(400, "attachment_invalid", f"{name}: data is not valid base64") from exc
        if not decoded:
            raise AppError(400, "attachment_invalid", f"{name}: empty file")
        limit = MAX_IMAGE_BYTES if media in IMAGE_TYPES else MAX_TEXT_BYTES
        if len(decoded) > limit:
            raise AppError(
                400,
                "attachment_too_large",
                f"{name}: {len(decoded):,} bytes; the limit for {media} is {limit:,}",
            )
        if media in TEXT_TYPES:
            try:
                decoded.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AppError(
                    400, "attachment_invalid", f"{name}: text attachments must be UTF-8"
                ) from exc
        out.append({"name": name, "media_type": media, "size": len(decoded), "data": raw})
    return out


# -------------------------------------------------------------------- shapes


def public_meta(meta: Any) -> dict[str, Any]:
    """`meta` as a client may see it: attachments without their bytes."""
    if not isinstance(meta, dict):
        return {}
    items = meta.get("attachments")
    if not isinstance(items, list) or not items:
        return dict(meta)
    public = dict(meta)
    public["attachments"] = [
        {
            "name": str(a.get("name", "")),
            "media_type": str(a.get("media_type", "")),
            "size": int(a.get("size", 0) or 0),
        }
        for a in items
        if isinstance(a, dict)
    ]
    return public


def attachment_bytes(meta: Any, index: int) -> tuple[bytes, str, str] | None:
    """`(bytes, media_type, name)` for one stored attachment, or None."""
    if not isinstance(meta, dict):
        return None
    items = meta.get("attachments")
    if not isinstance(items, list) or index < 0 or index >= len(items):
        return None
    item = items[index]
    if not isinstance(item, dict):
        return None
    try:
        data = base64.b64decode(str(item.get("data", "")), validate=False)
    except (ValueError, binascii.Error):
        return None
    return data, str(item.get("media_type") or "application/octet-stream"), str(item.get("name") or "file")


# --------------------------------------------------------------------- model


def _inline_text(name: str, media: str, raw_b64: str) -> str:
    try:
        text = base64.b64decode(raw_b64, validate=False).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error):
        return f"[attached file {name} could not be read]"
    cut = ""
    if len(text) > MAX_INLINE_CHARS:
        text = text[:MAX_INLINE_CHARS]
        cut = f"\n[… truncated; {name} continues beyond {MAX_INLINE_CHARS:,} characters]"
    fence = "json" if media == "application/json" else "csv" if "csv" in media else ""
    return f"Attached file `{name}` ({media}):\n```{fence}\n{text}{cut}\n```"


def model_content(
    text: str,
    attachments: list[dict[str, Any]] | None,
    *,
    include_images: bool = True,
) -> str | list[dict[str, Any]]:
    """What one user message looks like to the model.

    No attachments: the text, unchanged, as a plain string — the shape every
    existing test and every provider translation already handles. Text files
    are appended to the text. Images become `image_url` parts after the text,
    or a one-line placeholder when `include_images` is False (older history).
    """
    if not attachments:
        return text
    # Local import: `model_router` imports a great deal; this module must stay
    # cheap to import from `schemas`.
    from app.services.model_router import image_content_part

    body = text or ""
    parts: list[dict[str, Any]] = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        media = str(a.get("media_type", ""))
        name = str(a.get("name", "file"))
        if media in TEXT_TYPES:
            body = f"{body}\n\n{_inline_text(name, media, str(a.get('data', '')))}".strip()
        elif media in IMAGE_TYPES:
            if include_images and a.get("data"):
                parts.append(image_content_part(str(a["data"]), media_type=media))
            else:
                body = f"{body}\n\n[image attached: {name}]".strip()
    if not parts:
        return body
    return [{"type": "text", "text": body or "(see attached image)"}, *parts]
