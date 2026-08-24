"""DOM-level browser control: the table, the gate, and the loop around it.

The measured failure this suite is the regression net for, from one real
session driving a page by pixels:

    click(150, 272)
    click(136, 274)
    double_click(150, 272)

Three attempts at one target, and the third double-fired whatever the second
had already hit. The fix is that a browser action names an element rather than
a coordinate, and the properties that make that a product rather than a demo
are all asserted below.

* **One table.** The tools the model is handed, the risks the gate classifies,
  the paths the proxy calls and the fields it will put on the wire all come out
  of `services.browser.BROWSER_OPS`, and the real sidecar is parsed to prove
  the table is not fiction.
* **One execution path.** A DOM click goes through `simulation.perform` exactly
  as a pixel click does, so the risk gate, the approval flow and the undo log
  apply. Nothing in the loop holds a `DesktopManager`.
* **The gate can read the target.** A pixel `click` is named for the motion and
  the server never knew what was under the cursor. `browser_click` on
  `button "Delete account"` is held for a human whether the model declared
  anything or not — the one safety property the pixel lane cannot have.
* **The error contract survives.** `409 obscured` and `409 stale_ref` reach the
  model as different, actionable sentences instead of "the click failed", and a
  click that opened an `alert()` is reported as having *landed*.
* **`503` degrades to pixels** rather than failing the task.
* **A DOM step buys no screenshot.** The whole point is not paying for a
  photograph of a page it acted on structurally.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import ActionLog, Approval, BotDesktop
from app.services import browser as B
from app.services import simulation
from app.services.orchestrator import (
    AGENT_MAX_BROWSER_FALLBACKS,
    BROWSER_ACTIONS,
    BROWSER_TOOL_NAMES,
    RUN_AWAITING_HUMAN,
    TOOL_TASK_COMPLETE,
    agent_tools,
    desktop_protocol_block,
)
from app.services.risk import classify_action_risk, classify_label_risk, max_risk
from tests.services.conftest import actions_in, acts, call, turn

# ---------------------------------------------------------------------------
# 1. The table, and whether it describes the sidecar that actually exists
# ---------------------------------------------------------------------------


def _sidecar_source():
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[3].parent
        / "infra"
        / "bot-desktop"
        / "sidecar"
        / "browser.py"
    )
    if not path.exists():  # pragma: no cover - the CI lane copies only apps/
        pytest.skip("infra/ is not on disk in this lane")
    return path.read_text(encoding="utf-8")


def test_every_op_points_at_a_route_the_sidecar_actually_serves():
    """A path that does not exist is a tool that 404s for no visible reason."""
    import ast

    tree = ast.parse(_sidecar_source())
    served: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(
                decorator.func, ast.Attribute
            ):
                continue
            method = decorator.func.attr.upper()
            args = [a for a in decorator.args if isinstance(a, ast.Constant)]
            if method in ("GET", "POST") and args:
                served.add((method, "/browser" + str(args[0].value)))
    assert served, "could not read the sidecar's browser routes"
    for op in B.BROWSER_OPS:
        assert (op.method, op.path) in served, f"{op.name} -> {op.method} {op.path}"


def test_every_field_the_proxy_will_send_exists_on_the_sidecar_model():
    """A body key the sidecar has no field for is a silently ignored argument."""
    import ast

    tree = ast.parse(_sidecar_source())
    models: dict[str, ast.ClassDef] = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }

    def fields_of(name: str) -> set[str]:
        node = models.get(name)
        if node is None:
            return set()
        own = {
            s.target.id
            for s in node.body
            if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
        }
        for base in node.bases:
            if isinstance(base, ast.Name):
                own |= fields_of(base.id)
        return own

    # `path -> request model`, read off the route signatures rather than guessed.
    by_path: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        paths = [
            "/browser" + str(a.value)
            for d in node.decorator_list
            if isinstance(d, ast.Call)
            for a in d.args
            if isinstance(a, ast.Constant)
        ]
        annotations = [
            arg.annotation.id
            for arg in node.args.args
            if isinstance(arg.annotation, ast.Name)
        ]
        for path in paths:
            if annotations:
                by_path[path] = annotations[0]

    for op in B.BROWSER_OPS:
        if op.method == "GET":
            assert not op.fields, f"{op.name} is a GET and cannot carry a body"
            continue
        model = by_path.get(op.path)
        assert model, f"no request model found for {op.path}"
        unknown = set(op.fields) - fields_of(model)
        assert not unknown, f"{op.name} would send {unknown}, which {model} has no field for"


def test_the_field_whitelist_is_a_superset_of_the_tool_arguments():
    """Anything the model can pass must survive the proxy, or it silently vanishes."""
    for op in B.BROWSER_OPS:
        assert set(op.properties) <= set(op.fields), op.name
        assert set(op.required) <= set(op.properties), op.name


def test_request_body_drops_the_audit_annotation_and_anything_invented():
    op = B.op_for("browser_click")
    body = B.request_body(
        op, {"ref": "e1", "snapshot_id": "s2", "ref_label": 'button "Send"', "eval": "x"}
    )
    assert body == {"ref": "e1", "snapshot_id": "s2"}


def test_the_snapshot_default_is_the_economical_one():
    """A 200-element snapshot can cost more than the screenshot it replaces."""
    body = B.request_body(B.op_for("browser_snapshot"), {})
    assert body["viewport_only"] is True
    assert body["max_elements"] == B.SNAPSHOT_DEFAULTS["max_elements"]
    # …and every one of them is a knob the model can turn.
    override = B.request_body(
        B.op_for("browser_snapshot"), {"viewport_only": False, "max_elements": 400}
    )
    assert override["viewport_only"] is False and override["max_elements"] == 400
    properties = B.op_for("browser_snapshot").properties
    for knob in ("viewport_only", "max_elements", "name_filter", "role_filter"):
        assert knob in properties


def test_a_javascript_url_is_refused_before_it_leaves_the_api():
    """A `javascript:` navigation is the eval endpoint the sidecar refuses to have."""
    assert B.url_problem("browser_navigate", {"url": "javascript:alert(1)"})
    assert B.url_problem("browser_navigate", {"url": "data:text/html,x"})
    assert B.url_problem("browser_navigate", {"url": "mailto:a@b.test"})
    assert B.url_problem("browser_navigate", {"url": "https://example.test/x"}) is None
    assert B.url_problem("browser_navigate", {"url": "file:///home/nesq/a.html"}) is None
    assert B.url_problem("browser_click", {"url": "javascript:alert(1)"}) is None


# ---------------------------------------------------------------------------
# 2. The vocabulary the model is handed
# ---------------------------------------------------------------------------


def test_the_browser_tools_the_brief_asked_for_exist():
    offered = {t["function"]["name"] for t in agent_tools()}
    for name in (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_hover",
        "browser_extract",
        "browser_wait",
        "browser_tabs",
        "browser_back",
        "browser_forward",
        "browser_dialog",
    ):
        assert name in offered, name


def test_every_acting_browser_tool_can_declare_an_escalating_risk():
    """A DOM click on Send is a send. It gets the same lever a pixel click has.

    A *read* does not: declaring a `send` on `browser_snapshot` is meaningless,
    and every schema is re-sent on every model call, so an argument nothing can
    use is a per-step charge for nothing.
    """
    for tool in agent_tools():
        name = tool["function"]["name"]
        if name not in BROWSER_ACTIONS:
            continue
        risk = tool["function"]["parameters"]["properties"].get("risk")
        op = B.op_for(name)
        if op.observes:
            assert risk is None, name
            continue
        assert risk is not None, name
        assert set(risk["enum"]) == {"mutate", "send", "spend", "delete"}


def test_the_dom_tool_schemas_stay_affordable():
    """They are re-sent on every model call; verbosity here is a per-step charge.

    The first draft of this table cost ~4 600 prompt tokens a request, which on
    a forty-step run is comparable to the entire saving screenshot pruning was
    written to make. Trimming the descriptions took it to ~3 060, and this is
    the guard that keeps a well-meant sentence from quietly buying that back.

    The second number is the one that now matters. The descriptions are as
    short as they usefully get, so the next lane took the other lever: what is
    *sent*. Ten of these nineteen describe things that only exist once a page
    is open — hovering, waiting on a selector, answering a dialog, going back,
    and the four tab operations — so before the first `browser_snapshot` they
    are not advertised at all. See `services.context_budget.DOM_ENTRY_SET`.
    """
    import json

    from app.services.context_budget import ToolContext
    from app.services.orchestrator import agent_tools_for

    dom = json.dumps([t for t in agent_tools() if t["function"]["name"] in BROWSER_ACTIONS])
    # 3,250 rather than 3,200: the `risk` description was widened by ~50 tokens
    # across the declarable tools to say when NOT to declare. A model declared
    # `send` on a click that merely opened a LinkedIn profile, and because a
    # declared risk is escalate-only that parked the entire task waiting for a
    # person who had nothing to approve. Fifty tokens a call is a fair price for
    # not stopping the work; the ceiling moves deliberately, and only with a
    # reason written next to it.
    assert len(dom) // 4 < 3250, f"the DOM tool schemas are ~{len(dom) // 4} tokens a request"

    entry = agent_tools_for(ToolContext(desktop_running=True, browser_available=True))
    sent = json.dumps([t for t in entry if t["function"]["name"] in BROWSER_ACTIONS])
    # 1,850 for the same +50-tokens-a-tool reason as the 3,250 above.
    assert len(sent) // 4 < 1875, (
        f"the DOM tools actually sent before a page is open are ~{len(sent) // 4} tokens"
    )


def test_an_unadvertised_op_is_not_dispatchable_from_the_loop():
    """`browser_status` is reachable from the service layer and is not a tool."""
    assert "browser_status" in B.BROWSER_ACTIONS
    assert "browser_status" not in BROWSER_TOOL_NAMES
    assert "browser_status" not in {t["function"]["name"] for t in agent_tools()}


def test_the_prompt_advertises_the_dom_surface_and_says_when_to_leave_it():
    block = desktop_protocol_block()
    for action in BROWSER_ACTIONS:
        assert f"- {action} —" in block, action
    # DOM first…
    assert block.index("browser_snapshot") < block.index("- screenshot —")
    # …pixels for the things the accessibility tree cannot describe.
    for boundary in ("canvas", "CAPTCHA", "PDF", "video"):
        assert boundary in block, boundary
    # …and the real-site recoveries, named.
    for recovery in ("stale_ref", "obscured", "pending_dialog", "iframe"):
        assert recovery in block, recovery


# ---------------------------------------------------------------------------
# 3. Risk — the one thing a DOM lane can classify that a pixel lane cannot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("browser_snapshot", "observe"),
        ("browser_navigate", "observe"),
        ("browser_click", "observe"),
        ("browser_type", "observe"),
        ("browser_extract", "observe"),
        ("browser_tab_close", "mutate"),
        ("browser_dialog", "mutate"),
    ],
)
def test_browser_actions_classify_through_the_one_shared_classifier(action, expected):
    """Not through a second table. A `browser_*` name resolves in `risk.py`."""
    assert classify_action_risk(action) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ('button "Delete account"', "delete"),
        ('button "Send invoice"', "spend"),
        ('button "Send message"', "send"),
        ('button "Buy now"', "spend"),
        ('button "Publish"', "send"),
        # The reason this is a word match and not a substring match: every one
        # of these contains a keyword and none of them is a mutation.
        ('link "Learn more"', "observe"),
        ('textbox "Postcode"', "observe"),
        ('link "Shareholders"', "observe"),
        ('button "Search"', "observe"),
        ("", "observe"),
    ],
)
def test_an_elements_accessible_name_classifies_what_it_will_do(label, expected):
    assert classify_label_risk(label) == expected


def test_label_risk_can_only_escalate():
    assert max_risk("delete", classify_label_risk('link "Learn more"')) == "delete"
    assert max_risk("observe", classify_label_risk('button "Delete"')) == "delete"


def test_a_snapshot_is_parsed_back_into_the_names_the_gate_reads():
    snapshot = (
        'e1 heading "Nesq CDP Test Bench"\n'
        'text "A form, a list and a link."\n'
        'e2 textbox "Email address"\n'
        'e3 combobox "Plan" value="Free"\n'
        '  e4 option "Free" [selected]\n'
        "e11 button\n"
        'e13 link "Go to the detail page" -> /detail.html\n'
        "--- iframe https://example.com/ ---\n"
        'e15 button "Delete everything" [disabled]'
    )
    refs = B.parse_snapshot_refs(snapshot)
    assert refs["e2"] == ("textbox", "Email address")
    assert refs["e4"] == ("option", "Free")
    assert refs["e11"] == ("button", "")
    assert B.ref_label(refs, "e13") == 'link "Go to the detail page"'
    assert B.ref_label(refs, "e11") == "button"
    assert B.ref_label(refs, "e999") == ""
    # An element inside an iframe is addressable like any other.
    assert classify_label_risk(B.ref_label(refs, "e15")) == "delete"


# ---------------------------------------------------------------------------
# 4. The error contract, as the model reads it
# ---------------------------------------------------------------------------


def test_every_documented_error_code_has_a_remedy():
    """A code with no remedy is a model retrying the thing that just refused."""
    documented = {
        "url_not_allowed",
        "bad_selector",
        "unknown_key",
        "missing_selector",
        "unknown_ref",
        "stale_ref",
        "not_actionable",
        "obscured",
        "select_failed",
        "unknown_target",
        "no_dialog",
        "no_history_entry",
        "cdp_error",
        "navigation_failed",
        "browser_unavailable",
        "cdp_timeout",
        "wait_timeout",
    }
    assert documented <= set(B.ERROR_GUIDANCE)


def test_a_stale_ref_reaches_the_model_as_something_it_can_act_on():
    text = B.result_text(
        "browser_click",
        {
            "ok": False,
            "status": 409,
            "error": "stale_ref",
            "detail": "e10 now resolves to a different element",
            "expected": {"role": "button", "name": "Rename me"},
            "actual": {"role": "button", "name": "I was renamed"},
        },
    )
    assert "409 stale_ref" in text
    assert "Rename me" in text and "I was renamed" in text
    assert "browser_snapshot" in text
    assert "Nothing on the page changed" in text


def test_obscured_names_the_thing_on_top_and_says_to_dismiss_it():
    text = B.result_text(
        "browser_click",
        {
            "ok": False,
            "status": 409,
            "error": "obscured",
            "detail": 'e9 is covered by div "We use cookies"',
        },
    )
    assert "We use cookies" in text
    assert "dismiss" in text.lower()


def test_select_failed_lists_the_real_options_and_the_aria_recipe():
    text = B.result_text(
        "browser_select",
        {
            "ok": False,
            "status": 409,
            "error": "select_failed",
            "detail": "not_a_select",
            "options": ["Free", "Pro"],
        },
    )
    assert "Free" in text and "Pro" in text
    assert "browser_click" in text and "browser_snapshot" in text


def test_a_click_that_opened_a_dialog_is_reported_as_having_landed():
    """`ok: true` with a dialog. Retrying it would double-fire whatever it did."""
    text = B.result_text(
        "browser_click",
        {
            "ok": True,
            "ref": "e9",
            "role": "button",
            "name": "Send invoice",
            "pending_dialog": {"type": "confirm", "message": "Send this invoice?"},
        },
    )
    assert "ran on button" in text
    assert "Do NOT repeat" in text
    assert "browser_dialog" in text
    assert "Send this invoice?" in text


def test_a_timeout_caused_by_a_dialog_says_so():
    text = B.result_text(
        "browser_click",
        {
            "ok": False,
            "status": 504,
            "error": "cdp_timeout",
            "detail": "Input.dispatchMouseEvent did not answer in 10s",
            "pending_dialog": {"type": "alert", "message": "hi"},
        },
    )
    assert "browser_dialog" in text and "blocking" in text


def test_a_navigation_tells_the_model_its_refs_are_void():
    for action in ("browser_navigate", "browser_back", "browser_forward", "browser_reload"):
        text = B.result_text(action, {"ok": True, "url": "https://x.test", "title": "X"})
        assert "void" in text and "browser_snapshot" in text, action


def test_a_truncated_snapshot_says_how_much_it_is_not_showing():
    text = B.result_text(
        "browser_snapshot",
        {
            "ok": True,
            "snapshot_id": "s1",
            "url": "https://x.test",
            "title": "X",
            "interactive_total": 442,
            "matched": 442,
            "returned": 100,
            "truncated": True,
            "snapshot": 'e1 link "a"',
        },
    )
    assert "showing 100 of 442" in text
    assert "max_elements" in text and "name_filter" in text


def test_a_browser_result_cannot_flood_the_conversation():
    huge = "x" * 200_000
    text = B.result_text("browser_text", {"ok": True, "text": huge, "length": len(huge)})
    assert len(text) < B.RESULT_MAX_CHARS + 500
    assert "not shown" in text


# ---------------------------------------------------------------------------
# 5. The proxy
# ---------------------------------------------------------------------------


async def test_a_mock_deployment_answers_browser_unavailable(db, make_bot, user_a):
    """There is no Chromium, and the caller's correct response is the pixel API."""
    bot = await make_bot(user_a, name="Mocky")
    result = await simulation._desktop.browser_call(db, bot.id, "browser_snapshot", {})
    assert result["ok"] is False
    assert result["error"] == B.BROWSER_UNAVAILABLE
    assert result["status"] == 503
    assert "pixel" in B.result_text("browser_snapshot", result).lower()


async def test_a_desktop_that_is_not_running_answers_the_same_way(
    db, make_bot, user_a, monkeypatch
):
    bot = await make_bot(user_a, name="Downy")
    monkeypatch.setattr(simulation._desktop.settings, "bot_desktop_mode", "docker")
    result = await simulation._desktop.browser_call(db, bot.id, "browser_click", {"ref": "e1"})
    assert result["error"] == B.BROWSER_UNAVAILABLE and result["status"] == 503


async def test_an_unknown_op_is_refused_rather_than_guessed_at(db, make_bot, user_a):
    bot = await make_bot(user_a, name="Guessy")
    result = await simulation._desktop.browser_call(db, bot.id, "browser_eval", {"js": "x"})
    assert result["error"] == "unknown_browser_action"


async def test_the_proxy_keeps_the_status_and_the_body_the_sidecar_wrote(
    db, make_bot, user_a, monkeypatch, live_desktop
):
    """Not `raise_for_status`. The codes are the whole value of this lane."""
    bot = await live_desktop(user_a, "Proxy")
    monkeypatch.setattr(simulation._desktop.settings, "bot_desktop_mode", "docker")
    monkeypatch.setattr(simulation._desktop.settings, "nesq_sidecar_token", "test-sidecar-token")

    captured: dict = {}

    class _Response:
        status_code = 409

        def json(self):
            return {
                "ok": False,
                "error": "obscured",
                "detail": 'e9 is covered by div "banner"',
            }

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured.update(url=url, body=json, headers=headers)
            return _Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await simulation._desktop.browser_call(
        db, bot.id, "browser_click", {"ref": "e9", "ref_label": 'button "x"'}
    )

    assert result["status"] == 409
    assert result["error"] == "obscured"
    assert result["detail"] == 'e9 is covered by div "banner"'
    assert captured["url"].endswith("/browser/click")
    assert captured["body"] == {"ref": "e9"}
    assert captured["headers"]["X-Nesq-Sidecar-Token"] == "test-sidecar-token"


# ---------------------------------------------------------------------------
# 6. The loop
# ---------------------------------------------------------------------------


@pytest.fixture
def live_desktop(db, make_bot):
    """A bot whose desktop row says running, pointed at a control URL."""

    async def _make(user, name="Browsy", control_url="http://desktop.test:7910"):
        bot = await make_bot(user, name=name, daily_budget_usd=500.0)
        db.add(
            BotDesktop(
                bot_id=bot.id,
                state="running",
                control_url=control_url,
                stream_url="http://desktop.test:6901",
            )
        )
        await db.flush()
        return bot

    return _make


@pytest.fixture
def browser_sidecar(monkeypatch):
    """Answer `/browser/*` from a script, and record what was actually sent.

    Patches `DesktopManager.browser_call`, which is one layer *below* the
    chokepoint on purpose: everything the loop does still goes through
    `simulation.perform`, so the risk gate, the approval flow and the undo log
    are all exercised for real.
    """
    sent: list[tuple[str, dict]] = []
    replies: dict[str, list[dict]] = {}

    async def _call(_db, _bot_id, action, payload=None):
        sent.append((action, dict(payload or {})))
        queue = replies.get(action)
        if queue:
            return queue.pop(0) if len(queue) > 1 else dict(queue[0])
        return {"ok": True, "action": action, "status": 200}

    monkeypatch.setattr(simulation._desktop, "browser_call", _call)
    return type("Sidecar", (), {"sent": sent, "replies": replies})()


def _snapshot_reply(snapshot: str, snapshot_id: str = "s1") -> dict:
    return {
        "ok": True,
        "status": 200,
        "snapshot_id": snapshot_id,
        "target_id": "T1",
        "url": "https://shop.test/cart",
        "title": "Cart",
        "interactive_total": 3,
        "matched": 3,
        "returned": 3,
        "truncated": False,
        "frames": 1,
        "snapshot": snapshot,
    }


BENCH = (
    'e1 heading "Your cart"\n'
    'e2 textbox "Discount code"\n'
    'e3 link "Keep shopping" -> /shop\n'
    'e4 button "Delete account"'
)


async def test_a_dom_step_goes_through_the_chokepoint_and_costs_no_screenshot(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The headline. Navigate, snapshot, click a ref — and never take a picture."""
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH)]
    browser_sidecar.replies["browser_navigate"] = [
        {"ok": True, "status": 200, "url": "https://shop.test/cart", "title": "Cart"}
    ]
    browser_sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "link", "name": "Keep shopping"}
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_navigate", url="https://shop.test/cart")),
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Went back to the shop.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "empty my cart")

    assert actions_in(frames) == ["browser_navigate", "browser_snapshot", "browser_click"]
    # Not one screenshot. That is the saving, and it is the whole reason the
    # DOM lane exists next to the pixel one rather than on top of it.
    assert varying_screens["n"] == 0
    assert not any(
        part.get("type") == "image_url"
        for message in orchestrator.router.seen[-1]
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )
    assert "Went back to the shop." in done["message"]

    # Everything reached the sidecar through `simulation.perform`, so it is all
    # in the undo log with a real risk class on it.
    logged = (await db.execute(select(ActionLog))).scalars().all()
    assert [row.action for row in logged] == [
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
    ]


async def test_the_loop_pins_the_snapshot_and_names_what_it_clicks(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH, "s7")]
    browser_sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "link", "name": "Keep shopping"}
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "click keep shopping")

    click = next(payload for action, payload in browser_sidecar.sent if action == "browser_click")
    # Pinned, so the sidecar refuses a ref from an older snapshot instead of
    # acting on whatever now occupies that node.
    assert click["snapshot_id"] == "s7"
    # …and the label the gate reads. It never reaches Chromium: the proxy's own
    # whitelist drops it, which `test_request_body_drops_...` pins.
    assert click["ref_label"] == 'link "Keep shopping"'


async def test_a_navigation_voids_the_refs_but_not_what_they_were(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The sidecar's rule, mirrored — and the one thing deliberately kept.

    A navigation invalidates every ref, so the loop stops claiming it holds live
    ones. What it keeps is the *record* of what each ref was: `e3` was
    `link "Keep shopping"` on `https://shop.test/cart` in tab `T1`.

    That record is not a live reference and cannot be used as one. It is what
    `simulation._perform_browser` re-resolves by when the model reaches for a
    dead ref — and it is checked against the page before anything happens, which
    is what this asserts: the tab has moved on, so the answer is a refusal that
    names the two pages, not a click on whatever is there now.

    Erasing it here instead would leave the case that most needs recovery — a
    model still holding refs from before a navigation, which is the commonest
    way a ref dies — with nothing to recover from.
    """
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [
        _snapshot_reply(BENCH, "s2"),
        # The re-resolution's own look, taken after the tab moved on.
        {**_snapshot_reply('e9 link "Keep shopping" -> /shop', "s3"),
         "url": "https://shop.test/other"},
    ]
    browser_sidecar.replies["browser_navigate"] = [
        {"ok": True, "status": 200, "url": "https://shop.test/other", "title": "Other"}
    ]
    browser_sidecar.replies["browser_click"] = [
        {"ok": False, "status": 409, "error": "unknown_ref",
         "detail": "e3 is not from a live snapshot"}
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_navigate", url="https://shop.test/other")),
            acts("", call("browser_click", ref="e3")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "go")

    click = next(payload for action, payload in browser_sidecar.sent if action == "browser_click")
    # The provenance survived the navigation…
    assert click["ref_label"] == 'link "Keep shopping"'
    assert click[B.REF_PAGE_KEY] == "https://shop.test/cart"
    # …pinned to the snapshot the ref really came from, not to a later one.
    assert click["snapshot_id"] == "s2"
    # …and the recovery refused, because a same-named link on another page is a
    # different link. Exactly one click was attempted.
    assert sum(1 for action, _ in browser_sidecar.sent if action == "browser_click") == 1


async def test_clicking_a_delete_control_is_held_for_a_human(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The safety property the pixel lane cannot have.

    The model declared nothing. The server read `button "Delete account"` off
    the snapshot Chrome computed and held the step. A pixel `click(405, 359)`
    on the same button would have run.
    """
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH)]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e4")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "close my account")

    approval = (await db.execute(select(Approval))).scalars().one()
    assert approval.risk == "delete"
    # Named as the person reads it. The payload still carries the accessible
    # name verbatim — see `test_approved_browser_actions` — and the summary is
    # now the same sentence the reply would use for the same step.
    assert 'click "Delete account"' in approval.summary
    assert "it deletes something" in approval.summary
    assert done["awaiting_human"] is False
    assert "Approvals" in done["message"]
    # It has not run, and nothing pretends otherwise.
    assert not any(action == "browser_click" for action, _ in browser_sidecar.sent)


async def test_hovering_a_delete_button_is_not_held(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The gate fires on committing, not on the mouse passing over something.

    A gate that holds a step which did nothing is a gate people route around.
    """
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH)]
    browser_sidecar.replies["browser_hover"] = [
        {"ok": True, "status": 200, "ref": "e4", "role": "button", "name": "Delete account"}
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_hover", ref="e4")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Looked at it.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "hover the delete button")

    assert (await db.execute(select(Approval))).scalars().all() == []
    hover = next(p for a, p in browser_sidecar.sent if a == "browser_hover")
    # …and the audit trail still names what was hovered.
    assert hover["ref_label"] == 'button "Delete account"'


async def test_a_harmless_link_click_is_not_held(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The gate has to stay out of the way, or the model learns to use pixels."""
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH)]
    browser_sidecar.replies["browser_click"] = [
        {"ok": True, "status": 200, "ref": "e3", "role": "link", "name": "Keep shopping"}
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            acts("", call("browser_click", ref="e3")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Done.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "keep shopping")

    assert (await db.execute(select(Approval))).scalars().all() == []
    assert any(action == "browser_click" for action, _ in browser_sidecar.sent)


async def test_a_409_reaches_the_model_with_its_code_and_its_remedy(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_click"] = [
        {
            "ok": False,
            "status": 409,
            "error": "stale_ref",
            "detail": "e3 now resolves to a different element",
            "expected": {"role": "link", "name": "Keep shopping"},
            "actual": {"role": "link", "name": "Checkout"},
        }
    ]
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH, "s9")]
    orchestrator = agent_with(
        [
            acts("", call("browser_click", ref="e3")),
            # The recovery the error text asks for.
            acts("", call("browser_snapshot")),
            acts("", call(TOOL_TASK_COMPLETE, summary="Re-read the page.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "click it")

    handed_back = [
        m["content"]
        for m in orchestrator.router.seen[-1]
        if m.get("role") == "tool" and "stale_ref" in str(m.get("content"))
    ]
    assert handed_back, "the model was never told why the click was refused"
    text = str(handed_back[0])
    assert "409 stale_ref" in text
    assert "Checkout" in text
    assert "browser_snapshot" in text


async def test_a_503_degrades_to_pixels_in_the_same_turn(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The hybrid boundary, taken automatically rather than announced."""
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [
        {
            "ok": False,
            "status": 503,
            "error": B.BROWSER_UNAVAILABLE,
            "detail": "no answer on the debugging port",
        }
    ]
    orchestrator = agent_with(
        [
            acts("", call("browser_snapshot")),
            # …and the model carries on with coordinates.
            acts("", call("click", x=120, y=240)),
            acts("", call(TOOL_TASK_COMPLETE, summary="Did it by hand.")),
        ]
    )
    thread = await make_thread(user_a, [bot])

    frames, done = await turn(orchestrator, db, user_a, thread, "open the cart")

    assert actions_in(frames) == ["browser_snapshot", "click"]
    # A screenshot was taken *for* the failed browser call, so the model had
    # pixels to work from without having to ask.
    assert varying_screens["n"] >= 1
    prompt = "\n".join(
        str(part.get("text", ""))
        for message in orchestrator.router.seen[1]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict)
    )
    assert "DOM browser control is unavailable" in prompt
    assert "pixel tools" in prompt
    assert "Did it by hand." in done["message"]


async def test_a_model_that_will_not_take_the_fallback_is_stopped(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [
        {"ok": False, "status": 503, "error": B.BROWSER_UNAVAILABLE, "detail": "wedged"}
    ]
    orchestrator = agent_with(
        [acts("", call("browser_snapshot"))] * (AGENT_MAX_BROWSER_FALLBACKS + 1)
    )
    thread = await make_thread(user_a, [bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "look at the page")

    # Same two facts, said without the two pieces of house jargon. "DOM browser
    # control" and "the pixel tools" are how this repo names the two ways a bot
    # can drive a page; the person reading the reply has never seen either
    # phrase, and what they need is that the browser could not be driven and
    # that the bot went on asking for it anyway.
    assert "this desktop cannot be driven through the browser" in done["message"]
    assert "instead of working from the screen" in done["message"]
    # And the remedy, which the old wording left to the reader to infer.
    assert "Stopping and starting the desktop" in done["message"]


async def test_reading_the_page_over_and_over_without_acting_stops(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH)]
    orchestrator = agent_with([acts("", call("browser_snapshot"))] * 6)
    thread = await make_thread(user_a, [bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "look")

    assert "read the same page" in done["message"], done["message"]
    assert done["run_id"]


async def test_reading_several_DIFFERENT_things_is_diagnosis_not_idling(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """The reported bug: a lead-gen bot killed mid-diagnosis after three reads.

    Its last three steps were a snapshot, the page text, and a snapshot filtered
    for a website link — three different answers, which is how a page gets
    diagnosed. The guard counted reads rather than repeats and ended the run,
    reporting "I read the page 3 times in a row without acting on it" after the
    bot had done 32 successful actions.
    """
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [
        _snapshot_reply(BENCH, snapshot_id=f"s{i}") for i in range(1, 6)
    ]
    orchestrator = agent_with([acts("", call("browser_snapshot"))] * 4)
    thread = await make_thread(user_a, [bot])

    _frames, done = await turn(orchestrator, db, user_a, thread, "diagnose this page")

    assert "read the same page" not in done["message"], (
        "four reads that each returned something different were called idling: "
        + done["message"]
    )


async def test_the_dom_observation_never_claims_a_screen_was_shown(
    agent_with, db, user_a, make_thread, live_desktop, browser_sidecar, varying_screens
):
    """A model used to a frame after every action will otherwise narrate one."""
    bot = await live_desktop(user_a)
    browser_sidecar.replies["browser_snapshot"] = [_snapshot_reply(BENCH)]
    orchestrator = agent_with(
        [acts("", call("browser_snapshot")), acts("", call(TOOL_TASK_COMPLETE, summary="Read."))]
    )
    thread = await make_thread(user_a, [bot])

    await turn(orchestrator, db, user_a, thread, "look")

    prompt = "\n".join(
        str(part.get("text", ""))
        for message in orchestrator.router.seen[-1]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict)
    )
    assert "No screenshot was taken" in prompt


async def test_the_loop_holds_no_desktop_manager_of_its_own():
    """The chokepoint rule, restated for the newest surface.

    A caller that could take its own DOM click could take its own pixel click,
    and then the risk gate would have two paths around it. Asserted over the
    parse tree rather than the text, because the text says `DesktopManager`
    several times explaining why it must not hold one.
    """
    import ast
    import inspect

    from app.services import orchestrator

    tree = ast.parse(inspect.getsource(orchestrator))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "DesktopManager" not in names
    assert "_desktop" not in names and "_desktop" not in attributes
    for reached_directly in ("browser_call", "computer_action", "screenshot", "windows"):
        assert reached_directly not in attributes, reached_directly


async def test_a_browser_action_is_refused_when_the_bot_has_no_desktop_record(db, make_bot, user_a):
    """Preflight says so in a rehearsal, before anything is dialled."""
    bot = await make_bot(user_a, name="Rehearsy")
    assessment = await simulation.assess(
        db,
        simulation.Effect(
            kind="desktop", bot_id=bot.id, action="browser_click", input_data={"ref": "e1"}
        ),
    )
    assert assessment.problems


async def test_a_rehearsal_flags_a_url_the_browser_will_never_open(db, make_bot, user_a):
    bot = await make_bot(user_a, name="Rehearsy")
    assessment = await simulation.assess(
        db,
        simulation.Effect(
            kind="desktop",
            bot_id=bot.id,
            action="browser_navigate",
            input_data={"url": "javascript:alert(1)"},
        ),
    )
    assert any("not a URL this browser will open" in problem for problem in assessment.problems)


async def test_a_rehearsal_never_touches_the_browser(db, make_bot, user_a, live_desktop, monkeypatch):
    """`_execute` refuses to run inside a simulation, browser included."""
    bot = await live_desktop(user_a, "Dry")
    called: list[str] = []

    async def _boom(*_a, **_kw):  # pragma: no cover - must never be reached
        called.append("browser")
        return {"ok": True}

    monkeypatch.setattr(simulation._desktop, "browser_call", _boom)
    with simulation.SimulationContext(bot_id=bot.id):
        outcome = await simulation.perform(
            db,
            simulation.Effect(
                kind="desktop",
                bot_id=bot.id,
                action="browser_click",
                input_data={"ref": "e1", "ref_label": 'button "Delete account"'},
            ),
        )
    assert outcome.simulated is True
    assert called == []
    assert outcome.risk == "delete"


async def test_the_pixel_route_refuses_a_dom_action_rather_than_forwarding_it(
    authed, db, make_bot, user_a
):
    """`POST /desktop/action` is the pixel surface. There is no second DOM path.

    Without the guard a `browser_*` name would classify fine, pass the gate,
    and be POSTed to the sidecar's `/action`, which has never heard of it — a
    422 from two layers down wearing an action-failure costume.
    """
    bot = await make_bot(user_a, name="Pixel")
    response = await authed.post(
        f"/api/bots/{bot.id}/desktop/action",
        json={"action": "browser_click", "text": "e1"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "not_a_pixel_action"


def test_the_two_surfaces_do_not_share_a_name():
    assert not set(BROWSER_ACTIONS) & {"click", "type", "scroll", "key", "screenshot"}


def test_awaiting_human_is_still_reachable_from_a_browser_run():
    """Sanity: the DOM lane did not replace the authentication handoff."""
    assert RUN_AWAITING_HUMAN == "awaiting_human"
    assert uuid.UUID  # keeps the import honest for the fixtures above
