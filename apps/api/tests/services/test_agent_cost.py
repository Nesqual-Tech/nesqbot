"""What a desktop step costs, and where a downscaled click lands.

The session this file is the regression suite for: a LinkedIn task ran 35
desktop steps and spent a bot's entire $5.00 daily budget in one turn, and an
earlier "open LinkedIn and search" took about five minutes.

The money was not going on thinking. Nothing removed screenshots from the
*live* conversation — `_persistable_messages` strips them when a run parks for
a human, which is a different code path — so every model call re-sent every
frame taken so far. That is 1+2+...+35 image sends for a 35-step run: the bill
grew with the **square** of the step count, and so did the size of each
request, which is why the late steps crawled.

Two properties are asserted here and both have to hold for the fix to be real:

* **Bounded images.** A long run sends a constant number of images per
  request, not a growing one. `test_a_long_run_sends_a_bounded_number_of_images`
  is the guard that stops the quadratic coming back.
* **Correct clicks.** Making frames cheaper means downscaling them, which
  silently changes the coordinate space the model reports positions in. A click
  landing 20% off would be a far worse failure than a slow loop, so the mapping
  back onto true desktop pixels is checked with numbers, both as arithmetic and
  end to end through the loop into the sidecar call.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from app.services import simulation
from app.services.desktop import MOCK_SCREENSHOT_SIZE, ScreenGeometry, screenshot_image
from app.services.model_router import (
    REASONING_EFFORTS,
    TIER_PRICES,
    ToolCall,
    count_image_tokens,
    count_text_tokens,
    estimate_image_tokens,
)
from app.services.orchestrator import (
    AGENT_EFFORT_OPENING,
    AGENT_EFFORT_RECOVER,
    AGENT_EFFORT_STEP,
    AGENT_LOOP_TASK,
    AGENT_SCREENSHOT_HISTORY,
    AGENT_SCREENSHOT_OPTIONS,
    SCREEN_OMITTED_TEMPLATE,
    TOOL_TASK_COMPLETE,
    count_conversation_images,
    prune_screenshots,
)
from tests.services.conftest import acts, call, says, turn
from tests.services.screens import REAL_SCREEN, patch_real_sized_screens

# ---------------------------------------------------------------------------
# The unit of cost
# ---------------------------------------------------------------------------
#
# Image tokens are charged as a flat base plus one charge per 512x512 tile of
# the image *after* it has been fitted into a 2048 long edge / 768 short edge
# box. Both numbers below come out of that formula in `model_router`, and they
# are spelled out here because every dollar figure in this file is derived from
# them and a reader should be able to check the arithmetic.

#: A full-size 1280x800 PNG, which is what the loop used to send.
FULL_FRAME_TOKENS = estimate_image_tokens(*REAL_SCREEN)

#: The same screen captured at `max_width=1024`, which is what it sends now.
CAPPED_FRAME_TOKENS = estimate_image_tokens(
    AGENT_SCREENSHOT_OPTIONS["max_width"],
    round(REAL_SCREEN[1] * AGENT_SCREENSHOT_OPTIONS["max_width"] / REAL_SCREEN[0]),
)

#: USD per input token at the tier the agent loop runs on (`gpt-5.6-sol`).
REASON_INPUT_USD = Decimal(str(TIER_PRICES["reason"][0])) / Decimal(1_000_000)

#: Steps the scripted run below takes. The number from the reported session.
SCRIPTED_STEPS = 35


def test_the_published_image_prices_are_what_this_file_computes_from():
    """Guards the arithmetic every other assertion here depends on."""
    assert FULL_FRAME_TOKENS == 1105
    assert CAPPED_FRAME_TOKENS == 765


def test_capping_the_width_is_a_bytes_lever_more_than_a_token_lever():
    """A measured finding, recorded so nobody re-derives it the hard way.

    The sidecar's docstring quotes 10-20x for JPEG q75 plus downscaling, and
    that is true — of *bytes*, which is latency. Image *tokens* barely move,
    because the pricing formula already refits everything into a 768px short
    edge before it counts tiles. 1280x800 and 1024x640 both land on more than
    512 rows, so both pay for two rows of tiles.

    The cliff is at a 512px short edge, not at 1024px wide: capture 1280x800 at
    `max_width=819` and the short edge is exactly 512, one row of tiles, 425
    tokens. That is 2.6x off the full frame against 1.44x for the shipped
    default — but at 0.64x scale, small UI text starts costing clicks, and a
    misread search box is more expensive than the tokens. The default stays at
    1024; this test is the note explaining the number that was left on the
    table and where to find it.
    """
    assert FULL_FRAME_TOKENS / CAPPED_FRAME_TOKENS == pytest.approx(1.44, abs=0.01)
    assert estimate_image_tokens(819, 512) == 425


# ---------------------------------------------------------------------------
# 1. Pruning, as arithmetic
# ---------------------------------------------------------------------------


def _observation(step: int, text: str = "the screen") -> dict:
    """An observation message shaped the way `_observation_message` builds one."""
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"Desktop step {step}: `click` ran."},
            {
                "type": "text",
                "text": (
                    f"Attached is the screen as it is right now (1024x640), "
                    f"taken after desktop step {step}."
                ),
            },
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{text}"}},
            {"type": "text", "text": "3 step(s) left in this run."},
        ],
    }


def test_pruning_keeps_only_the_newest_frames():
    convo = [{"role": "system", "content": "be good"}] + [_observation(n) for n in range(1, 6)]

    removed = prune_screenshots(convo, keep=2)

    assert removed == 3
    assert count_conversation_images(convo) == 2
    # The two that survive are the *newest* two, not the first two.
    assert "image_url" in json.dumps(convo[-1])
    assert "image_url" in json.dumps(convo[-2])
    assert "image_url" not in json.dumps(convo[1:4])


def test_pruning_replaces_the_picture_with_a_line_that_says_which_step_it_was():
    """A vanished image with no note is a model reasoning about a screen it never saw."""
    convo = [_observation(7), _observation(8)]

    prune_screenshots(convo, keep=1)

    texts = [part["text"] for part in convo[0]["content"] if part["type"] == "text"]
    assert SCREEN_OMITTED_TEMPLATE.format(step=7) in texts
    assert not any("Attached is the screen" in text for text in texts)


def test_pruning_keeps_every_factual_line():
    """Only the pixels are expensive. The action result is the record of the run."""
    convo = [_observation(3), _observation(4)]

    prune_screenshots(convo, keep=1)

    blob = json.dumps(convo[0])
    assert "Desktop step 3: `click` ran." in blob
    assert "3 step(s) left in this run." in blob


def test_pruning_is_idempotent():
    """It runs before every model call, so a second pass must cost nothing."""
    convo = [_observation(n) for n in range(1, 5)]

    prune_screenshots(convo, keep=2)
    snapshot = json.loads(json.dumps(convo))

    assert prune_screenshots(convo, keep=2) == 0
    assert convo == snapshot


def test_pruning_leaves_a_conversation_with_nothing_to_prune_alone():
    convo = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

    assert prune_screenshots(convo, keep=2) == 0
    assert convo == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]


def test_a_hand_built_image_message_still_gets_told_the_picture_is_gone():
    """The fallback path: no recognisable 'attached' sentence to swap out."""
    convo = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}],
        },
        _observation(2),
    ]

    prune_screenshots(convo, keep=1)

    assert count_conversation_images(convo) == 1
    assert "[Screenshot from desktop step ?" in json.dumps(convo[0])


# ---------------------------------------------------------------------------
# 2. The headline: a long run does not grow its own bill
# ---------------------------------------------------------------------------


def _images_and_placeholders(request: list[dict]) -> tuple[int, int]:
    """`(images sent, images that would have been sent before the fix)`.

    The second number is exact rather than modelled: every frame the pruner
    removed left a placeholder behind in the same message, so counting both
    reconstructs the unpruned request the old code would have sent.
    """
    images = count_conversation_images(request)
    placeholders = sum(
        1
        for message in request
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(part, dict)
        and str(part.get("text", "")).startswith("[Screenshot from desktop step")
    )
    return images, images + placeholders


@pytest.fixture
async def long_run(agent_with, db, user_a, make_thread, agent_bot, monkeypatch):
    """One scripted 35-step desktop run against a 1280x800 mock desktop.

    The bot's budget is deliberately large: this measures what a run *costs*,
    and a cap firing part-way through would measure the cap instead.
    """
    patch_real_sized_screens(monkeypatch)
    script = [acts("", call("click", x=100, y=100 + n)) for n in range(SCRIPTED_STEPS)]
    script.append(acts("", call(TOOL_TASK_COMPLETE, summary="Finished the search.")))
    orchestrator = agent_with(script)
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread, "open linkedin and search")
    return orchestrator.router


async def test_a_long_run_sends_a_bounded_number_of_images(long_run):
    """The guard. No request may carry more frames than the history setting.

    This is the property that makes the cost linear in steps instead of
    quadratic, and it is asserted per request rather than in aggregate so the
    failure names the request that broke it.
    """
    per_request = [count_conversation_images(request) for request in long_run.seen]

    assert len(long_run.seen) >= SCRIPTED_STEPS, "the scripted run did not actually run"
    assert max(per_request) <= AGENT_SCREENSHOT_HISTORY, (
        f"a request carried {max(per_request)} images; the cap is {AGENT_SCREENSHOT_HISTORY}"
    )
    # And it really is a *bound*, not an accident of a short run: the loop
    # reaches the cap and stays there rather than climbing.
    assert per_request.count(AGENT_SCREENSHOT_HISTORY) > SCRIPTED_STEPS // 2


async def test_pruning_removes_the_quadratic(long_run):
    """Before and after, in tokens and dollars, for the reported 35-step run.

    `before` is not a model of the old behaviour — it is counted off the
    placeholders the pruner left behind, so it is exactly the request the
    previous code would have sent.
    """
    before_images = sum(_images_and_placeholders(r)[1] for r in long_run.seen)
    after_images = sum(_images_and_placeholders(r)[0] for r in long_run.seen)
    calls = len(long_run.seen)

    # Quadratic in the step count: the sum 1+2+...+n over the follow-up calls.
    assert before_images == calls * (calls - 1) // 2
    # Linear: at most `AGENT_SCREENSHOT_HISTORY` per call.
    assert after_images <= AGENT_SCREENSHOT_HISTORY * calls

    before_usd = before_images * FULL_FRAME_TOKENS * REASON_INPUT_USD
    after_usd = after_images * CAPPED_FRAME_TOKENS * REASON_INPUT_USD

    # The text half, so the figures below are a whole-prompt total rather than
    # only the pixels. Exact for `after`; for `before` it is this same text
    # (the pruner swaps one sentence for another of similar length and touches
    # nothing else), which is within a per-cent of the real thing.
    text_tokens = sum(count_text_tokens(request) for request in long_run.seen)
    before_total = before_images * FULL_FRAME_TOKENS + text_tokens
    after_total = after_images * CAPPED_FRAME_TOKENS + text_tokens

    # Captured unless the suite is run with `-s`, and printed on failure. This
    # is the measurement itself: `pytest -s -k removes_the_quadratic` is how
    # you get the headline number back without reading the assertions.
    print(
        f"\n  {calls} model calls over {SCRIPTED_STEPS} desktop steps"
        f"\n  before: {before_images:>5} images  "
        f"{before_images * FULL_FRAME_TOKENS:>8,} image tokens  ${before_usd:.2f}"
        f"\n  after:  {after_images:>5} images  "
        f"{after_images * CAPPED_FRAME_TOKENS:>8,} image tokens  ${after_usd:.2f}"
        f"\n  reduction: {before_usd / after_usd:.1f}x on images"
        f"\n  whole prompt (text {text_tokens:,} tokens included):"
        f"\n    before {before_total:>9,} input tokens  "
        f"${before_total * REASON_INPUT_USD:.2f}"
        f"\n    after  {after_total:>9,} input tokens  "
        f"${after_total * REASON_INPUT_USD:.2f}"
        f"   (${after_total * REASON_INPUT_USD / SCRIPTED_STEPS:.4f} per desktop step)"
    )

    # Asserted in TOKENS, not dollars. This test measures pruning, and pruning
    # is a property of the prompt, not of the price list — pinning it to $3.00
    # made it fail the day `TIER_PRICES["reason"]` moved from the OpenAI-kind
    # account to Grok, which changed nothing about how many images are sent.
    # The dollar figures stay in the printout above, where they inform without
    # deciding.
    before_image_tokens = before_images * FULL_FRAME_TOKENS
    after_image_tokens = after_images * CAPPED_FRAME_TOKENS
    assert before_image_tokens > 600_000
    assert after_image_tokens < 60_000
    assert before_image_tokens / after_image_tokens > 13

    # Where the remaining cost sits, so the next person does not have to work
    # it out: `AGENT_SCREENSHOT_HISTORY` is a straight multiplier on the after
    # figure. Dropping it from 2 to 1 halves it again, to roughly 26x. Two is
    # kept because a before-and-after pair is what lets a model see that a
    # dropdown opened, and thirteen cents on a $5 budget is not the thing to
    # trade that for.
    assert after_images == pytest.approx(AGENT_SCREENSHOT_HISTORY * calls, rel=0.1)


async def test_the_images_actually_in_the_requests_are_priced_as_claimed(long_run):
    """`count_image_tokens` over the real requests, not over a model of them.

    Ties the dollar figures above to the estimator the cost ledger bills from:
    if the frames in the conversation were not the size this file assumes, this
    is what says so.
    """
    per_request = [count_image_tokens(request) for request in long_run.seen]
    charged = [tokens for tokens in per_request if tokens]

    assert charged, "no request carried an image at all"
    assert max(charged) <= AGENT_SCREENSHOT_HISTORY * CAPPED_FRAME_TOKENS
    assert set(charged) <= {CAPPED_FRAME_TOKENS, AGENT_SCREENSHOT_HISTORY * CAPPED_FRAME_TOKENS}


async def test_the_loop_captures_at_the_agent_settings_not_at_full_size(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """The frames the loop pays for are JPEG, capped and (by default) colour."""
    seen: list[dict] = []
    real = simulation._desktop.screenshot

    async def _screenshot(db_, bot_id, **options):
        seen.append(dict(options))
        return await real(db_, bot_id, **options)

    monkeypatch.setattr(simulation._desktop, "screenshot", _screenshot)
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts("", call(TOOL_TASK_COMPLETE, summary="done")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    # `simulation._screenshot_options` renames `format` to the manager's `fmt`
    # keyword and forwards nothing the sidecar has no query parameter for.
    expected = {
        "fmt": AGENT_SCREENSHOT_OPTIONS["format"],
        "quality": AGENT_SCREENSHOT_OPTIONS["quality"],
        "max_width": AGENT_SCREENSHOT_OPTIONS["max_width"],
        "grayscale": AGENT_SCREENSHOT_OPTIONS["grayscale"],
    }
    assert seen, "the loop took no screenshot"
    assert all(options == expected for options in seen)
    assert AGENT_SCREENSHOT_OPTIONS["format"] == "jpeg"
    assert AGENT_SCREENSHOT_OPTIONS["max_width"] == 1024


async def test_the_capture_options_stay_out_of_the_step_transcript(
    agent_with, db, user_a, make_thread, agent_bot
):
    """`Looked at the screen`, not `screenshot(format='jpeg', max_width=1024, …)`.

    How the loop pays for a frame is not something the bot chose to do, and a
    person reading what their bot did should not have to skip past it.

    The assertion moved off `screenshot()` because the step log is written in
    English now: the capture options cannot leak into a phrase that has no
    arguments in it at all, which is a stronger guarantee than the one this
    test used to make and the same one it was after.
    """
    orchestrator = agent_with(
        [
            acts("", call("screenshot")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Looked.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    assert "Looked at the screen" in done["message"]
    assert "max_width" not in done["message"]
    assert "jpeg" not in done["message"]
    assert "screenshot" not in done["message"], "the tool's name is not the reader's word for it"


# ---------------------------------------------------------------------------
# 3. Coordinate mapping — the risk the downscale creates
# ---------------------------------------------------------------------------


def test_a_full_size_capture_maps_to_itself():
    """The identity has to be exact, or every un-downscaled deployment drifts."""
    geometry = ScreenGeometry.from_screenshot(
        {"ok": True, "width": 1280, "height": 800, "screen_width": 1280, "screen_height": 800}
    )

    assert geometry.is_identity
    for point in ((0, 0), (1, 1), (405, 359), (1279, 799)):
        assert geometry.to_screen(*point) == point


def test_a_downscaled_capture_maps_back_onto_the_real_desktop():
    """The numbers, spelled out: 1280x800 captured at 1024 wide is 0.8 scale."""
    geometry = ScreenGeometry.from_screenshot(
        {
            "ok": True,
            "width": 1024,
            "height": 640,
            "screen_width": 1280,
            "screen_height": 800,
            "scale": 0.8,
        }
    )

    assert not geometry.is_identity
    assert geometry.scale_x == 0.8
    # The click from the reported session, reported against a 1024-wide view.
    assert geometry.to_screen(405, 359) == (506, 449)
    # Corners stay corners.
    assert geometry.to_screen(0, 0) == (0, 0)
    assert geometry.to_screen(1023, 639) == (1279, 799)


def test_an_unmapped_coordinate_would_land_twenty_percent_off():
    """Why this file exists, stated as a number.

    Without the mapping, a click the model aims at the middle of a 1024-wide
    view lands a fifth of the screen up and to the left of where it meant.
    """
    geometry = ScreenGeometry.from_screenshot(
        {"ok": True, "width": 1024, "height": 640, "screen_width": 1280, "screen_height": 800}
    )
    raw = (512, 320)

    mapped = geometry.to_screen(*raw)

    assert mapped == (640, 400)
    assert mapped[0] - raw[0] == 128  # 20% of 640
    assert mapped[1] - raw[1] == 80


@pytest.mark.parametrize("cap", [1024, 960, 819, 640, 512, 320])
def test_the_mapping_round_trips_within_a_pixel_at_every_scale(cap):
    """Every true pixel maps to an image pixel that maps back to within one.

    A half-pixel bias per axis is what naive `x/scale` produces, and at a 4x
    downscale that is two real pixels of drift on every single click — enough
    to miss a checkbox. Pixel *centres* are mapped instead; this is the proof.
    """
    width, height = REAL_SCREEN
    scale = cap / width
    geometry = ScreenGeometry.from_screenshot(
        {
            "ok": True,
            "width": cap,
            "height": round(height * scale),
            "screen_width": width,
            "screen_height": height,
        }
    )

    worst = 0
    for true_x in range(0, width, 7):
        image_x = min(int(true_x * scale), cap - 1)
        back_x, _ = geometry.to_screen(image_x, 0)
        worst = max(worst, abs(back_x - true_x))
    # One image pixel covers `1/scale` true pixels, so that is the floor on
    # what any mapping can promise. Nothing here does worse than it.
    assert worst <= round(1 / scale)


def test_a_cropped_capture_maps_through_its_offset():
    """The sidecar can crop as well as scale; both have to come back out."""
    geometry = ScreenGeometry.from_screenshot(
        {
            "ok": True,
            "width": 400,
            "height": 300,
            "screen_width": 1280,
            "screen_height": 800,
            "region": {"x": 200, "y": 100, "w": 800, "h": 600},
        }
    )

    assert geometry.scale_x == 0.5
    assert geometry.to_screen(0, 0) == (200, 100)
    assert geometry.to_screen(200, 150) == (600, 400)


def test_the_mapping_clamps_to_the_screen():
    """Rounding at the far edge must not produce an off-screen point."""
    geometry = ScreenGeometry.from_screenshot(
        {"ok": True, "width": 1024, "height": 640, "screen_width": 1280, "screen_height": 800}
    )

    assert geometry.to_screen(5000, 5000) == (1279, 799)
    assert geometry.to_screen(-50, -50) == (0, 0)


def test_a_screenshot_that_failed_yields_the_identity():
    """An unknown rescale is never guessed at."""
    assert ScreenGeometry.from_screenshot({"ok": False, "error": "no display"}).is_identity
    assert ScreenGeometry.from_screenshot(None).is_identity
    assert ScreenGeometry.from_screenshot({"ok": True}).is_identity


def test_every_point_argument_is_mapped_and_nothing_else_is():
    """`drag` has two points; `scroll` has a point and an amount that is not one."""
    geometry = ScreenGeometry.from_screenshot(
        {"ok": True, "width": 640, "height": 400, "screen_width": 1280, "screen_height": 800}
    )

    assert geometry.to_screen_arguments({"x": 10, "y": 20, "to_x": 30, "to_y": 40}) == {
        "x": 20,
        "y": 40,
        "to_x": 60,
        "to_y": 80,
    }
    assert geometry.to_screen_arguments(
        {"x": 10, "y": 20, "direction": "down", "amount": 3}
    ) == {"x": 20, "y": 40, "direction": "down", "amount": 3}
    assert geometry.to_screen_arguments({"text": "hello"}) == {"text": "hello"}


def test_the_identity_returns_the_arguments_untouched():
    assert ScreenGeometry().to_screen_arguments({"x": 405, "y": 359}) == {"x": 405, "y": 359}


# ---------------------------------------------------------------------------
# 4. Coordinate mapping, end to end through the loop
# ---------------------------------------------------------------------------


async def test_a_click_on_a_downscaled_screen_reaches_the_sidecar_in_real_pixels(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """The whole chain, with numbers: model sees 160x100, xdotool gets 320x200.

    The model is shown a half-scale view of the mock desktop and clicks the
    centre of it. What must arrive at the sidecar is the centre of the *real*
    screen — if the rescale leaked through, this is the test that fails, and it
    fails by exactly the factor of the downscale.
    """
    patch_real_sized_screens(monkeypatch, screen=MOCK_SCREENSHOT_SIZE, max_width=160)
    sent: list[dict] = []

    async def _action(db_, bot_id, action, payload):
        sent.append({"action": action, **payload})
        return {"ok": True, "action": action}

    monkeypatch.setattr(simulation._desktop, "computer_action", _action)
    orchestrator = agent_with(
        [
            acts("", call("screenshot")),
            acts("", call("click", x=80, y=50)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Clicked the middle.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread)

    clicks = [entry for entry in sent if entry["action"] == "click"]
    assert clicks, "no click reached the sidecar"
    # 160x100 image of a 320x200 desktop: the middle of the view is the middle
    # of the screen, and it is not (80, 50).
    assert (clicks[0]["x"], clicks[0]["y"]) == (160, 100)
    # And the transcript a human reads says where the pointer actually went.
    # Same fact, said the way the reply says everything now: the coordinates are
    # the user's own screen, so they stay — it is the function-call syntax
    # around them that went.
    assert "Clicked at (160, 100)" in done["message"]


async def test_the_model_is_told_which_pixels_to_speak_in(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """A scaled view says so, and gives the model the size it should measure in."""
    patch_real_sized_screens(monkeypatch, screen=MOCK_SCREENSHOT_SIZE, max_width=160)
    orchestrator = agent_with(
        [
            acts("", call("screenshot")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Looked.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    blob = json.dumps(orchestrator.router.seen[-1])
    assert "scaled view of a 320x200 desktop" in blob
    assert "160 wide by 100 tall" in blob


async def test_a_full_size_capture_says_nothing_about_scaling(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """No rescale, no note. An unnecessary warning is one more thing to misread."""
    patch_real_sized_screens(monkeypatch, screen=MOCK_SCREENSHOT_SIZE, max_width=None)
    orchestrator = agent_with(
        [
            acts("", call("screenshot")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Looked.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    assert "scaled view" not in json.dumps(orchestrator.router.seen[-1])


async def test_a_held_step_holds_the_real_coordinates_for_the_human(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """What a reviewer approves must be what would run.

    An approval carrying the model's scaled coordinates would ask a person to
    authorise a click at a point nothing will ever be clicked at, and then run
    something else.
    """
    from sqlalchemy import select

    from app.models import Approval

    patch_real_sized_screens(monkeypatch, screen=MOCK_SCREENSHOT_SIZE, max_width=160)
    orchestrator = agent_with(
        [
            acts("", call("screenshot")),
            acts("", ToolCall(id=str(uuid.uuid4()), name="click", arguments={"x": 80, "y": 50, "risk": "send"})),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    rows = await db.execute(select(Approval).where(Approval.bot_id == agent_bot.id))
    approval = rows.scalars().one()
    step = approval.payload["steps"][0]
    assert (step["x"], step["y"]) == (160, 100)
    assert "risk" not in step


# ---------------------------------------------------------------------------
# 5. Reasoning effort
# ---------------------------------------------------------------------------


def test_the_shipped_efforts_are_what_the_deployments_accept():
    """The probe result, frozen as a test.

    Probed live on 2026-08-23 against the swedencentral account, sending
    function tools exactly as the loop does: `gpt-5.6-sol` and `gpt-5.6-terra`
    take only an omitted effort or `"none"` next to tools and answer 400 to the
    graded scale; `gpt-5.4-mini` takes all of it. The full table and the
    latency numbers are on `REASONING_EFFORTS` in `model_router`.

    If these defaults ever drift back to `low`/`medium`/`high` on the reason
    tier, every loop step will 400 once and then silently run with no effort
    hint at all — which looks exactly like the setting working.
    """
    assert AGENT_EFFORT_STEP == "none", "ordinary steps must suppress reasoning"
    assert AGENT_EFFORT_RECOVER == "", "the reason tier cannot be asked for more than default"
    assert AGENT_EFFORT_OPENING in REASONING_EFFORTS or AGENT_EFFORT_OPENING == ""


async def test_an_ordinary_step_does_not_pay_to_reason(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """"Click the search box" is not a thing to deliberate over."""
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts("", call("click", x=2, y=2)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Clicked twice.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    # The opening call decides whether to act at all and runs on the mini tier,
    # where the graded scale is available; everything after it is a step taken
    # against a screen the model can already see.
    assert orchestrator.router.efforts[0] == AGENT_EFFORT_OPENING
    assert set(orchestrator.router.efforts[1:]) == {AGENT_EFFORT_STEP}


async def test_a_step_after_a_failure_is_allowed_to_think(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """The one place in the loop where reasoning has earned its latency."""

    async def _refuse(db_, bot_id, action, payload):
        return {"ok": False, "action": action, "error": "xdotool: no display"}

    monkeypatch.setattr(simulation._desktop, "computer_action", _refuse)
    orchestrator = agent_with([], tail=acts("", call("click", x=1, y=1)))
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    # Only the loop's own calls. A run that ends by failing rather than by
    # calling `task_complete` makes one more call afterwards — the closing
    # summary, on the nano tier with no effort and no tools — and that is not a
    # step, so it does not belong in an assertion about how steps think.
    loop_efforts = [
        effort
        for task, effort in zip(
            orchestrator.router.tasks, orchestrator.router.efforts, strict=True
        )
        if task == AGENT_LOOP_TASK
    ]
    assert loop_efforts[-1] == AGENT_EFFORT_RECOVER
    assert loop_efforts[-1] != AGENT_EFFORT_STEP


async def test_a_narrating_model_is_re_prompted_without_the_suppression(
    agent_with, db, user_a, make_thread, agent_bot, varying_screens
):
    """The re-prompt is the product's original bug. It is allowed to reason."""
    orchestrator = agent_with(
        [
            says("I'm going to start by opening the site."),
            acts("", call("click", x=1, y=1)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Clicked.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    await turn(orchestrator, db, user_a, thread)

    assert orchestrator.router.efforts[1] == AGENT_EFFORT_RECOVER


# The resumed-run case lives in `test_agent_loop.py`, next to the `take_over`
# helper that parks a run realistically:
# `test_resume_continues_the_same_task_after_a_fresh_look_at_the_screen`.


# ---------------------------------------------------------------------------
# 6. Cost, said out loud
# ---------------------------------------------------------------------------


async def test_every_step_reports_what_it_cost(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """A run that can eat a day's budget has to say so while it is happening."""
    patch_real_sized_screens(monkeypatch)
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts("", call("click", x=2, y=2)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Clicked twice.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, _ = await turn(orchestrator, db, user_a, thread)

    costs = [data for name, data in frames if name == "cost"]
    assert len(costs) == 2, "one frame per follow-up model call"
    for frame in costs:
        assert frame["cost_usd"] > 0
        assert frame["image_tokens"] > 0
        assert frame["image_tokens"] <= frame["input_tokens"]
        assert frame["budget_usd"] == pytest.approx(float(agent_bot.daily_budget_usd))
        assert frame["spent_today_usd"] >= frame["cost_usd"]
    # The running turn cost only ever goes up, and the step number tracks the loop.
    assert [f["step"] for f in costs] == sorted(f["step"] for f in costs)
    assert costs[0]["turn_cost_usd"] < costs[-1]["turn_cost_usd"]


async def test_the_cost_frame_names_the_image_half_of_the_prompt(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """`image_tokens` against `input_tokens` is the whole story of a vision turn."""
    patch_real_sized_screens(monkeypatch)
    orchestrator = agent_with(
        [
            acts("", call("click", x=1, y=1)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Clicked.")),
        ]
    )
    thread = await make_thread(user_a, [agent_bot])

    frames, _ = await turn(orchestrator, db, user_a, thread)

    frame = next(data for name, data in frames if name == "cost")
    assert frame["image_tokens"] == CAPPED_FRAME_TOKENS


# ---------------------------------------------------------------------------
# 7. The screenshot payload, whichever format it arrives in
# ---------------------------------------------------------------------------


def test_the_image_reader_handles_both_response_shapes():
    """The sidecar answers `png_base64` for PNG and `image_base64` for JPEG."""
    assert screenshot_image({"ok": True, "png_base64": "abc"}) == ("abc", "image/png")
    assert screenshot_image({"ok": True, "image_base64": "xyz", "mime": "image/jpeg"}) == (
        "xyz",
        "image/jpeg",
    )
    assert screenshot_image({"ok": False, "error": "nope"}) == ("", "")
    assert screenshot_image(None) == ("", "")


async def test_the_http_screenshot_route_still_gets_a_full_size_png(db, make_bot, user_a):
    """docs/API.md pins `png_base64`, and a human wants the real pixels."""
    bot = await make_bot(user_a, name="Viewer", system_prompt="hi")

    result = await simulation._desktop.screenshot(db, bot.id)

    assert result["ok"]
    assert result["png_base64"]
    assert (result["width"], result["height"]) == MOCK_SCREENSHOT_SIZE
    assert result["scale"] == 1.0


async def test_the_reply_to_a_long_run_leads_with_the_result(
    agent_with, db, user_a, make_thread, agent_bot, monkeypatch
):
    """The shape of the reply, on the run that produced the complaint.

    The verdict on the shipped version was *"it's telling me things, i don't
    care"*: thirty-five numbered `click(x=405, y=359) — ran` lines and almost
    no result. What a person should now read first is what the bot found.
    """
    patch_real_sized_screens(monkeypatch)
    script = [acts("", call("click", x=100, y=100 + n)) for n in range(SCRIPTED_STEPS)]
    script.append(
        acts(
            "",
            call(
                TOOL_TASK_COMPLETE,
                summary=(
                    "I searched LinkedIn for heads of operations in Berlin and found 18 "
                    "matching people. I opened the first six profiles and noted their "
                    "current role and company. I did not message anyone — that needs your "
                    "approval, and I have not drafted anything yet."
                ),
            ),
        )
    )
    orchestrator = agent_with(script)
    thread = await make_thread(user_a, [agent_bot])

    _, done = await turn(orchestrator, db, user_a, thread, "open linkedin and search")
    message = done["message"]

    head, _, rest = message.partition("\n")
    assert head.startswith("I searched LinkedIn for heads of operations in Berlin")
    # Nothing between the result and the folded log except the log's own summary.
    assert rest.strip().startswith("**What I did")
    # The count is still there and it is still only there: the fold's summary
    # line is the one place in the reply that is allowed to talk about how many
    # of anything ran. `desktop actions` became `steps on the desktop` when the
    # log stopped being a list of tool calls.
    assert f"{SCRIPTED_STEPS + 1} steps on the desktop" in message
    assert "click(" not in message, "no function calls anywhere in a reply, folded or not"
    # The transcript is all there, just not in the way.
    assert len(step_log_lines(message)) == SCRIPTED_STEPS + 1
    print("\n" + "-" * 70 + "\n" + message[:900] + "\n" + "-" * 70)


def step_log_lines(reply: str) -> list[str]:
    body = reply.split("**What I did", 1)[1].split(chr(10), 1)[1]
    return [line for line in (raw.strip() for raw in body.splitlines()) if line]
