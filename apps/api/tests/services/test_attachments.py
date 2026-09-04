"""Attachments: what is accepted, what a client sees, and what the model sees."""

from __future__ import annotations

import base64

import pytest

from app.errors import AppError
from app.services import attachments as att
from tests.conftest import _client_for  # noqa: F401 - keeps the fixture graph warm

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# --------------------------------------------------------------- validate


def test_validate_normalises_and_measures():
    out = att.validate_attachments(
        [
            {"name": "  shot.PNG ", "media_type": "IMAGE/JPG; charset=x", "data": f"data:image/jpeg;base64,{PNG}"},
            {"name": "", "media_type": "text/csv", "data": b64("a,b\n1,2\n")},
        ]
    )
    assert out[0]["media_type"] == "image/jpeg"
    assert out[0]["name"] == "shot.PNG"
    assert out[0]["data"] == PNG  # the data-URL prefix is gone
    assert out[0]["size"] == 70
    assert out[1]["name"] == "attachment-2"
    assert out[1]["size"] == 8


def test_validate_passes_through_nothing():
    assert att.validate_attachments(None) == []
    assert att.validate_attachments([]) == []


@pytest.mark.parametrize(
    "item, code",
    [
        ({"name": "x", "media_type": "application/pdf", "data": PNG}, "attachment_type_unsupported"),
        ({"name": "x", "media_type": "image/png", "data": "%%%"}, "attachment_invalid"),
        ({"name": "x", "media_type": "image/png", "data": ""}, "attachment_invalid"),
        ({"name": "x", "media_type": "text/plain", "data": base64.b64encode(b"\xff\xfe").decode()}, "attachment_invalid"),
        ({"name": "x", "media_type": "text/plain", "data": b64("a" * (att.MAX_TEXT_BYTES + 1))}, "attachment_too_large"),
    ],
)
def test_validate_rejects_with_a_code(item, code):
    with pytest.raises(AppError) as excinfo:
        att.validate_attachments([item])
    assert excinfo.value.code == code
    assert excinfo.value.status_code == 400


def test_validate_caps_the_count():
    with pytest.raises(AppError) as excinfo:
        att.validate_attachments([{"name": "x", "media_type": "image/png", "data": PNG}] * (att.MAX_ATTACHMENTS + 1))
    assert excinfo.value.code == "too_many_attachments"


# ----------------------------------------------------------------- shapes


def test_public_meta_strips_bytes_and_keeps_everything_else():
    meta = {"handoff_to": "b", "attachments": [{"name": "a.png", "media_type": "image/png", "size": 3, "data": PNG}]}
    public = att.public_meta(meta)
    assert public == {"handoff_to": "b", "attachments": [{"name": "a.png", "media_type": "image/png", "size": 3}]}
    assert "data" in meta["attachments"][0]  # not mutated


def test_public_meta_tolerates_junk():
    assert att.public_meta(None) == {}
    assert att.public_meta("nope") == {}
    assert att.public_meta({"attachments": "not a list"}) == {"attachments": "not a list"}


def test_attachment_bytes_round_trips():
    meta = {"attachments": [{"name": "n.csv", "media_type": "text/csv", "data": b64("x,y")}]}
    assert att.attachment_bytes(meta, 0) == (b"x,y", "text/csv", "n.csv")
    assert att.attachment_bytes(meta, 1) is None
    assert att.attachment_bytes(meta, -1) is None
    assert att.attachment_bytes({}, 0) is None


# ------------------------------------------------------------------ model


def test_model_content_is_a_plain_string_without_attachments():
    assert att.model_content("hello", None) == "hello"
    assert att.model_content("hello", []) == "hello"


def test_text_files_are_inlined_under_a_heading():
    content = att.model_content("Summarise", [{"name": "q.csv", "media_type": "text/csv", "data": b64("a,b\n1,2")}])
    assert isinstance(content, str)
    assert content.startswith("Summarise")
    assert "Attached file `q.csv` (text/csv):" in content
    assert "```csv\na,b\n1,2\n```" in content


def test_long_text_files_are_cut_and_say_so():
    content = att.model_content("", [{"name": "big.txt", "media_type": "text/plain", "data": b64("z" * 50_000)}])
    assert isinstance(content, str)
    assert "truncated" in content
    assert len(content) < 50_000


def test_images_become_content_parts_after_the_text():
    content = att.model_content("What is this?", [{"name": "p.png", "media_type": "image/png", "data": PNG}])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "What is this?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{PNG}"


def test_images_degrade_to_a_placeholder_when_not_included():
    content = att.model_content("Look", [{"name": "p.png", "media_type": "image/png", "data": PNG}], include_images=False)
    assert content == "Look\n\n[image attached: p.png]"


def test_an_image_with_no_text_still_has_a_text_part():
    content = att.model_content("", [{"name": "p.png", "media_type": "image/png", "data": PNG}])
    assert content[0]["text"]


# ------------------------------------------------------------ orchestrator


async def test_the_turn_hands_the_model_the_image(agent_with, agent_bot, user_a, make_thread, db):
    """The router sees `image_url` parts on the user message it was sent with,
    and a stored user message keeps its attachments in `meta`."""
    from app.models import Message
    from tests.services.conftest import says

    orchestrator = agent_with([says("A single pixel.")])
    thread = await make_thread(user_a, [agent_bot])
    frames = [
        f
        async for f in orchestrator.handle_user_message_stream(
            db,
            user=user_a,
            thread=thread,
            content="What is this image?",
            attachments=[{"name": "p.png", "media_type": "image/png", "size": 70, "data": PNG}],
        )
    ]
    assert any(name == "done" for name, _ in frames)
    router = orchestrator.router
    assert router.seen, "the model was never called"
    user_turns = [m for m in router.seen[0] if m["role"] == "user"]
    assert user_turns, router.seen[0]
    last = user_turns[-1]["content"]
    assert isinstance(last, list), last
    assert last[0]["text"] == "What is this image?"
    assert any(p.get("type") == "image_url" for p in last)

    rows = (await db.execute(__import__("sqlalchemy").select(Message).where(Message.thread_id == thread.id))).scalars().all()
    stored = next(m for m in rows if m.role == "user")
    assert stored.meta["attachments"][0]["name"] == "p.png"


async def test_older_images_are_replaced_by_placeholders(agent_with, agent_bot, user_a, make_thread, db):
    """Only the last `HISTORY_IMAGE_MESSAGES` user messages keep their pixels."""
    from tests.services.conftest import says

    thread = await make_thread(user_a, [agent_bot])
    image = [{"name": "p.png", "media_type": "image/png", "size": 70, "data": PNG}]
    orchestrator = agent_with([says("one"), says("two"), says("three"), says("four")])
    for i in range(att.HISTORY_IMAGE_MESSAGES + 2):
        async for _ in orchestrator.handle_user_message_stream(
            db, user=user_a, thread=thread, content=f"image {i}", attachments=image
        ):
            pass
    final = orchestrator.router.seen[-1]
    user_turns = [m["content"] for m in final if m["role"] == "user"]
    with_pixels = [c for c in user_turns if isinstance(c, list)]
    placeholders = [c for c in user_turns if isinstance(c, str) and "[image attached" in c]
    assert len(with_pixels) == att.HISTORY_IMAGE_MESSAGES
    assert len(placeholders) == 2
