"""DOM-level browser control — the one table the whole API drives it from.

A bot that works on the web has been doing it by looking at a JPEG and guessing
pixels. One real session produced `click(150,272)`, `click(136,274)`,
`double_click(150,272)` — three attempts at a single target, and the last one
double-fired whatever it did land on. The sidecar's `/browser/*` lane fixes the
*perception* half: a page arrives as `e17 button "Create account"` lines
computed by Chrome's own accessibility engine, and an action names an element
instead of a coordinate. This module is the API half.

**What the win actually is.** Not bytes. Measured on the sidecar image, a
200-element snapshot is ~3 000 text tokens against ~1 300 vision tokens for a
1024px JPEG of the same page, so a careless snapshot is *more* expensive than
the screenshot it replaces. The win is that every line carries a `ref` the
model can act on, so the click lands the first time. That is why
`SNAPSHOT_DEFAULTS` is economical rather than complete and why `viewport_only`,
`max_elements`, `name_filter` and `role_filter` are all exposed as tool
parameters: the sidecar lane measured Wikipedia at 12 672 B full, 4 169 B
viewport-only and 2 965 B at `max_elements=60`, and the model has to be able to
reach for those.

**Screenshots do not go away.** `<canvas>` apps, CAPTCHAs, PDF.js and `<video>`
expose nothing useful in the accessibility tree, and neither does any
non-browser window on the desktop. `BROWSER_UNAVAILABLE` is the third case: a
wedged Chromium answers `503` and the agent loop degrades to pixels rather than
failing the task.

**One table.** `BROWSER_OPS` is the only enumeration of the surface. The tool
schemas the model is handed, the risk each op classifies as, the fields the
proxy is allowed to put on the wire, and the dispatch in
`simulation._perform_desktop` are all derived from it, so the vocabulary the
prompt advertises and the vocabulary that actually runs cannot drift — the same
discipline `orchestrator.DESKTOP_ACTIONS` already holds the pixel surface to.

Nothing in here performs an effect or holds a client. It is a table, some
schemas, and the pure functions that turn a sidecar response into the sentence
a model reads.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings

_SETTINGS = get_settings()

# ---------------------------------------------------------------------------
# Snapshot economy
# ---------------------------------------------------------------------------
#
# The sidecar's own defaults are 200 elements and 40 text nodes, which is the
# right default for a human running curl and the wrong one for something that
# pays per token on every step. These are the defaults the *model* gets when it
# calls `browser_snapshot` with no arguments; every one of them is overridable
# on the call, and the rendered result always says how much was left out so a
# truncated page is never silently a short one.

#: What a bare `browser_snapshot` asks the sidecar for. `viewport_only` is the
#: interesting choice: it is what is on screen, which is both the cheapest
#: snapshot and the one that matches how a person reads a page. A model that
#: needs the rest of the document passes `viewport_only=false`, and the result
#: line tells it how many elements it is not being shown.
SNAPSHOT_DEFAULTS: dict[str, Any] = {
    "max_elements": max(int(_SETTINGS.agent_browser_snapshot_max_elements), 1),
    "viewport_only": bool(_SETTINGS.agent_browser_snapshot_viewport_only),
    "max_text_nodes": max(int(_SETTINGS.agent_browser_snapshot_max_text), 0),
}

#: Ceiling on the text one browser result contributes to the conversation.
#: The sidecar already caps a rendered snapshot at 24 KB; this is the second,
#: tighter bound that also covers `browser_text` and `browser_extract`, whose
#: payloads are bounded by the *page*, not by the sidecar.
RESULT_MAX_CHARS = 12_000

#: URL schemes the sidecar will navigate to. Re-stated here so a bad URL is a
#: preflight problem a rehearsal can show, rather than a round trip that comes
#: back `400`. A `javascript:` navigation is an eval endpoint wearing a hat.
ALLOWED_URL_RE = re.compile(r"^(https?://|about:blank$|file:///home/nesq/)")


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserOp:
    """One `/browser/*` endpoint, as everything in the API needs to see it.

    `fields` is a whitelist, not documentation: `DesktopManager.browser_call`
    forwards only these keys. That is what lets the agent loop annotate an
    effect with `ref_label` — so the approval a human reads says
    `browser_click(ref='e9', ref_label='button "Send"')` instead of a bare ref —
    without that annotation ever reaching Chromium.
    """

    #: The tool/action name. Namespaced so it can never collide with a pixel
    #: primitive: `click` and `browser_click` are different things and a model
    #: that confuses them would be clicking a coordinate it never computed.
    name: str
    #: Sidecar path under the desktop's control URL.
    path: str
    #: `GET` or `POST`. The two listing endpoints are GETs and take no body.
    method: str
    #: The one line the model reads about this op.
    hint: str
    #: Risk before any declared escalation. See `risk.classify_action_risk`.
    risk: str
    #: JSON Schema properties for the model-facing tool.
    properties: dict[str, Any] = field(default_factory=dict)
    #: Required tool arguments.
    required: tuple[str, ...] = ()
    #: Keys the proxy may put in the request body. Superset of `properties`
    #: where a default is filled in server-side, subset where an argument is
    #: for the audit trail rather than for Chromium.
    fields: tuple[str, ...] = ()
    #: Reads the page and changes nothing. Feeds the loop's idle detector.
    observes: bool = False
    #: Any ref this op holds becomes void afterwards, because the page changed
    #: underneath it. The model is told, every time, in the result text.
    invalidates_refs: bool = False
    #: Offered to the model. False keeps an endpoint reachable from the service
    #: layer without spending a tool slot on it.
    advertise: bool = True


# These three fragments are repeated across ten, five and nineteen tool schemas
# respectively, and every character of them is re-sent on every model call. The
# measured cost of the DOM surface is worth stating plainly: at their first,
# chattier wording the browser tools were ~4 600 prompt tokens *per request*,
# which on a forty-step run is roughly what the whole screenshot-pruning fix
# saved. Terse here is not style.
#: `target_id` is in the *field* whitelist of almost every op, so the service
#: layer can address a background tab, but it is offered to the model on the two
#: tools that cannot mean anything without it. The model's mental model is one
#: active tab: `browser_tab_activate` switches, everything else acts on
#: whatever is in front. That is one fewer argument to get wrong on nineteen
#: tools, and nineteen fewer copies of its description in every request.
_TARGET = {
    "target_id": {"type": "string", "description": "A tab id from browser_tabs."}
}
_REF = {
    "ref": {"type": "string", "description": 'A ref from the last snapshot, e.g. "e17".'},
    "snapshot_id": {
        "type": "string",
        "description": 'Its snapshot, e.g. "s3". Filled in for you; refs from an older one are refused.',
    },
}

#: Every `/browser/*` endpoint the API knows about, in the order the model
#: reads them. Ordered by workflow — arrive, look, act, read, navigate,
#: recover — because this is the order `orchestrator.desktop_protocol_block()`
#: renders them in.
BROWSER_OPS: tuple[BrowserOp, ...] = (
    BrowserOp(
        name="browser_navigate",
        path="/browser/navigate",
        method="POST",
        hint=(
            'go to a URL in the bot\'s Chromium — {"url": "https://…"}. '
            "http(s), about:blank and file:///home/nesq/ only"
        ),
        risk="observe",
        properties={
            "url": {"type": "string", "description": "http(s)://…, about:blank, or file:///home/nesq/…"},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "none"],
                "description": "How long to wait before answering. Default load.",
            },
            "timeout_ms": {"type": "integer", "description": "1000-120000, default 30000."},
        },
        required=("url",),
        fields=("url", "wait_until", "timeout_ms", "target_id"),
        invalidates_refs=True,
    ),
    BrowserOp(
        name="browser_snapshot",
        path="/browser/snapshot",
        method="POST",
        hint=(
            "read the page as `ref role \"name\"` lines you can act on — {} for the "
            "visible part, or narrow with name_filter / role_filter"
        ),
        risk="observe",
        properties={
            "max_elements": {
                "type": "integer",
                "description": (
                    f"Cap on interactive elements, 1-1000. Default "
                    f"{SNAPSHOT_DEFAULTS['max_elements']}; a big article has 400+."
                ),
            },
            "viewport_only": {
                "type": "boolean",
                "description": "Only what is on screen. Default true; false for the whole document.",
            },
            "name_filter": {
                "type": "string",
                "description": "Substring of the accessible name — the cheapest way to find one control.",
            },
            "role_filter": {
                "type": "string",
                "description": 'Only this ARIA role, e.g. "button", "textbox", "link".',
            },
            "include_text": {
                "type": "boolean",
                "description": "Include static text lines. Default true.",
            },
            "max_text_nodes": {
                "type": "integer",
                "description": (
                    "Cap on static text lines, 0-400. Default "
                    f"{SNAPSHOT_DEFAULTS['max_text_nodes']}."
                ),
            },
        },
        fields=(
            "max_elements",
            "viewport_only",
            "name_filter",
            "role_filter",
            "include_text",
            "max_text_nodes",
            "target_id",
        ),
        observes=True,
    ),
    BrowserOp(
        name="browser_click",
        path="/browser/click",
        method="POST",
        hint='click an element by reference — {"ref": "e9"}',
        risk="observe",
        properties={
            **_REF,
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "click_count": {"type": "integer", "description": "2 for a double click. 1-3."},
            "modifiers": {
                "type": "integer",
                "description": "Held keys as a bitmask: 1=alt 2=ctrl 4=meta 8=shift.",
            },
        },
        required=("ref",),
        fields=("ref", "snapshot_id", "button", "click_count", "modifiers"),
    ),
    BrowserOp(
        name="browser_type",
        path="/browser/type",
        method="POST",
        hint='type into a field by reference — {"ref": "e2", "text": "a@b.com"}',
        risk="observe",
        properties={
            **_REF,
            "text": {"type": "string", "description": "What to type."},
            "clear": {"type": "boolean", "description": "Empty the field first. Default true."},
            "submit": {
                "type": "boolean",
                "description": "Press Enter afterwards. That can submit a form — declare risk if it sends.",
            },
        },
        required=("ref", "text"),
        fields=("ref", "snapshot_id", "text", "clear", "submit"),
    ),
    BrowserOp(
        name="browser_select",
        path="/browser/select",
        method="POST",
        hint=(
            'choose options in a native <select> — {"ref": "e3", "values": ["Pro"]}. '
            "An ARIA combobox is a div: click it, re-snapshot, click the option"
        ),
        risk="observe",
        properties={
            **_REF,
            "values": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Option value, label or visible text. Any of the three matches.",
            },
        },
        required=("ref", "values"),
        fields=("ref", "snapshot_id", "values"),
    ),
    BrowserOp(
        name="browser_hover",
        path="/browser/hover",
        method="POST",
        hint='hover an element to open a menu it reveals — {"ref": "e5"}',
        risk="observe",
        properties=dict(_REF),
        required=("ref",),
        fields=("ref", "snapshot_id"),
    ),
    BrowserOp(
        name="browser_scroll",
        path="/browser/scroll",
        method="POST",
        hint=(
            'scroll the page — {"direction": "down", "amount_px": 600} — or bring one '
            'element into view — {"ref": "e40"}'
        ),
        risk="observe",
        properties={
            "ref": {
                "type": "string",
                "description": "Scroll this element into view instead of scrolling the page.",
            },
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount_px": {"type": "integer", "description": "Pixels, 1-20000. Default 600."},
        },
        fields=("ref", "direction", "amount_px", "target_id"),
    ),
    BrowserOp(
        name="browser_key",
        path="/browser/key",
        method="POST",
        hint='press one key on the page — {"key": "Enter"}',
        risk="observe",
        properties={
            "key": {
                "type": "string",
                "description": (
                    "Enter Tab Escape Backspace Delete Home End PageUp PageDown Space "
                    "ArrowUp ArrowDown ArrowLeft ArrowRight."
                ),
            },
            "modifiers": {"type": "integer", "description": "1=alt 2=ctrl 4=meta 8=shift."},
        },
        required=("key",),
        fields=("key", "modifiers", "target_id"),
    ),
    BrowserOp(
        name="browser_text",
        path="/browser/text",
        method="POST",
        hint='read the page\'s visible text — {} for all of it, or {"selector": "#main"}',
        risk="observe",
        properties={
            "selector": {"type": "string", "description": "CSS selector; omit for the whole page."},
            "max_chars": {"type": "integer", "description": "100-200000. Default 4000."},
        },
        fields=("selector", "max_chars", "target_id"),
        observes=True,
    ),
    BrowserOp(
        name="browser_extract",
        path="/browser/extract",
        method="POST",
        hint=(
            'pull a repeating list off the page — {"selector": ".row", "fields": '
            '[{"name": "title", "selector": "h3"}]}'
        ),
        risk="observe",
        properties={
            "selector": {"type": "string", "description": "CSS selector for one row."},
            "fields": {
                "type": "array",
                "description": "What to read out of each row. Omit for the row's own text.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "selector": {"type": "string", "description": "Relative to the row."},
                        "attr": {"type": "string", "description": 'Attribute instead of text, e.g. "href".'},
                    },
                    "required": ["name"],
                },
            },
            "limit": {"type": "integer", "description": "Rows, 1-1000. Default 100."},
        },
        required=("selector",),
        fields=("selector", "fields", "limit", "target_id"),
        observes=True,
    ),
    BrowserOp(
        name="browser_wait",
        path="/browser/wait",
        method="POST",
        hint=(
            'wait for the page to catch up — {"until": "selector", "selector": ".results"} '
            'or {"until": "text", "text": "Saved"}'
        ),
        risk="observe",
        properties={
            "until": {"type": "string", "enum": ["load", "selector", "text", "gone"]},
            "selector": {"type": "string", "description": "Required for until=selector or gone."},
            "text": {"type": "string", "description": "Required for until=text."},
            "state": {"type": "string", "enum": ["visible", "attached", "hidden", "detached"]},
            "timeout_ms": {"type": "integer", "description": "100-120000. Default 10000."},
        },
        required=("until",),
        fields=("until", "selector", "text", "state", "timeout_ms", "target_id"),
        observes=True,
    ),
    BrowserOp(
        name="browser_tabs",
        path="/browser/tabs",
        method="GET",
        hint="list the open tabs and which one is active — takes no input",
        risk="observe",
        observes=True,
    ),
    BrowserOp(
        name="browser_tab_new",
        path="/browser/tabs/new",
        method="POST",
        hint='open a new tab — {"url": "https://…"}',
        risk="observe",
        properties={
            "url": {"type": "string", "description": "Same allowed schemes as browser_navigate."},
            "activate": {"type": "boolean", "description": "Bring it to the front. Default true."},
        },
        fields=("url", "activate"),
    ),
    BrowserOp(
        name="browser_tab_activate",
        path="/browser/tabs/activate",
        method="POST",
        hint='switch to a tab — {"target_id": "…"} from browser_tabs',
        risk="observe",
        properties=dict(_TARGET),
        required=("target_id",),
        fields=("target_id",),
    ),
    BrowserOp(
        name="browser_tab_close",
        path="/browser/tabs/close",
        method="POST",
        hint='close a tab — {"target_id": "…"}',
        # Not `observe`: a closed tab takes its unsaved page state with it, and
        # the audit trail should not read as though the bot only looked.
        risk="mutate",
        properties=dict(_TARGET),
        required=("target_id",),
        fields=("target_id",),
        invalidates_refs=True,
    ),
    BrowserOp(
        name="browser_back",
        path="/browser/back",
        method="POST",
        hint="go back one page in history — takes no input",
        risk="observe",
        fields=("target_id",),
        invalidates_refs=True,
    ),
    BrowserOp(
        name="browser_forward",
        path="/browser/forward",
        method="POST",
        hint="go forward one page in history — takes no input",
        risk="observe",
        fields=("target_id",),
        invalidates_refs=True,
    ),
    BrowserOp(
        name="browser_reload",
        path="/browser/reload",
        method="POST",
        hint="reload the current page — takes no input",
        risk="observe",
        properties={
            "ignore_cache": {"type": "boolean", "description": "Hard reload. Default false."},
        },
        fields=("ignore_cache", "target_id"),
        invalidates_refs=True,
    ),
    BrowserOp(
        name="browser_dialog",
        path="/browser/dialog",
        method="POST",
        hint=(
            'answer a blocking alert()/confirm()/prompt() — {"accept": true}. '
            "Nothing else on the page works until you do"
        ),
        # A confirm() is the second half of a destructive action often enough
        # that recording it as an observation would be wrong. It does not gate;
        # declare `risk` when the dialog is confirming a send or a delete.
        risk="mutate",
        properties={
            "accept": {"type": "boolean", "description": "true = OK, false = Cancel."},
            "prompt_text": {"type": "string", "description": "Answer for a prompt() box."},
        },
        required=("accept",),
        fields=("accept", "prompt_text", "target_id"),
    ),
    BrowserOp(
        name="browser_status",
        path="/browser/status",
        method="GET",
        hint="is Chromium answering, and what tabs does it have — takes no input",
        risk="observe",
        observes=True,
        # Deliberately not in the model's vocabulary. `browser_snapshot`
        # answers "is the browser up" at the moment it matters, with a `503`
        # the loop already turns into a pixel fallback, so a separate
        # are-you-there tool would only ever be a wasted step. It stays in the
        # table because the table is what `DesktopManager.browser_call`
        # dispatches from, and an operator debugging a wedged desktop wants it.
        advertise=False,
    ),
)

BROWSER_OPS_BY_NAME: dict[str, BrowserOp] = {op.name: op for op in BROWSER_OPS}

#: Every browser action name, advertised or not. `simulation._perform_desktop`
#: routes on this, so an op that is in the table is dispatchable.
BROWSER_ACTIONS: frozenset[str] = frozenset(BROWSER_OPS_BY_NAME)

#: The subset the model is handed as tools, in table order.
ADVERTISED_OPS: tuple[BrowserOp, ...] = tuple(op for op in BROWSER_OPS if op.advertise)

#: `action -> risk`, merged into `risk.ACTION_RISKS` so there is still exactly
#: one classifier. Without this every `browser_*` name would fall to the
#: `mutate` default, and the audit log would say a bot mutated something when
#: it read a page.
BROWSER_ACTION_RISKS: dict[str, str] = {op.name: op.risk for op in BROWSER_OPS}

#: Reads that change nothing, for the agent loop's idle detector.
BROWSER_OBSERVATIONS: frozenset[str] = frozenset(op.name for op in BROWSER_OPS if op.observes)

#: Ops whose *target* is classified, not just their motion. A pixel `click` is
#: named for the movement and the server has no idea what is under the cursor;
#: a DOM click names an element whose accessible name Chrome computed. That is
#: the one safety property the pixel lane can never have: the agent loop
#: attaches `ref_label` from the last snapshot, and
#: `simulation._assess_desktop` runs it through `risk.classify_label_risk`, so
#: clicking `button "Delete account"` is held for a human even when the model
#: declared nothing.
#:
#: Only the three that commit something. `browser_hover` and `browser_scroll`
#: also take a ref and also carry a `ref_label` into the audit trail, but
#: escalating on it would hold a step for approval because the mouse passed
#: over a Delete button — a gate that fires on a step which did nothing is a
#: gate people learn to route around.
BROWSER_TARGETED: frozenset[str] = frozenset(
    {"browser_click", "browser_type", "browser_select"}
)


def op_for(action: str) -> BrowserOp | None:
    return BROWSER_OPS_BY_NAME.get(action)


def is_browser_action(action: str) -> bool:
    return action in BROWSER_OPS_BY_NAME


def request_body(op: BrowserOp, payload: dict[str, Any] | None) -> dict[str, Any]:
    """The JSON body for one op — whitelisted, defaults filled, nothing else.

    `op.fields` is the whitelist, so two classes of key never reach Chromium:
    `ref_label` — the annotation the agent loop puts on the effect so an
    approval and the undo log say *what* was clicked — and anything else a
    caller invented, which is dropped rather than forwarded so a model cannot
    reach a sidecar parameter by guessing its name.
    """
    given = dict(payload or {})
    body = {key: given[key] for key in op.fields if given.get(key) is not None}
    if op.name == "browser_snapshot":
        for key, value in SNAPSHOT_DEFAULTS.items():
            body.setdefault(key, value)
    if op.name == "browser_text":
        body.setdefault("max_chars", 4000)
    return body


def url_problem(action: str, payload: dict[str, Any] | None) -> str | None:
    """A preflight complaint about a navigation URL, or None.

    The sidecar enforces the same allowlist and answers `400 url_not_allowed`,
    so this is not the boundary — it is what makes a bad URL visible in a
    rehearsal, where no request is made at all. `javascript:` and `data:` are
    the ones that matter: a `javascript:` navigation is the eval endpoint the
    sidecar deliberately does not have.
    """
    if action not in ("browser_navigate", "browser_tab_new"):
        return None
    url = str((payload or {}).get("url") or "").strip()
    if not url:
        return None if action == "browser_tab_new" else "no URL was given to navigate to"
    if not ALLOWED_URL_RE.match(url):
        return (
            f"'{url[:80]}' is not a URL this browser will open — only http(s)://, "
            "about:blank and file:///home/nesq/ are allowed"
        )
    return None


# ---------------------------------------------------------------------------
# Reading a snapshot back
# ---------------------------------------------------------------------------

#: One rendered snapshot line: indent, ref, role, and the accessible name in
#: quotes. Value, href and flags follow and are not captured — this exists to
#: recover *what an element is*, not to re-parse the whole grammar.
_SNAPSHOT_LINE_RE = re.compile(r'^\s*(e\d{1,9})\s+(\S+)(?:\s+"([^"]*)")?')


def parse_snapshot_refs(snapshot: str) -> dict[str, tuple[str, str]]:
    """`ref -> (role, accessible name)` out of a rendered `lines` snapshot.

    Used only to classify what an action is about to touch. A name containing a
    double quote parses short, which can only *fail to* escalate — the model's
    own declared `risk` and the server-side classifier both still apply, so the
    worst case is that this optimisation does not fire.
    """
    found: dict[str, tuple[str, str]] = {}
    for line in (snapshot or "").splitlines():
        match = _SNAPSHOT_LINE_RE.match(line)
        if match:
            found[match.group(1)] = (match.group(2), match.group(3) or "")
    return found


#: Where the agent loop parks the accessible name of the element an action is
#: about to touch. Never sent to the sidecar (see `request_body`); it exists so
#: the risk gate, the approval a human reads, the undo log and the step
#: transcript all say `browser_click(ref='e9', ref_label='button "Send"')`
#: rather than a bare reference nobody can interpret after the fact.
REF_LABEL_KEY = "ref_label"


def label_in(payload: dict[str, Any] | None) -> str:
    """The `ref_label` a caller attached, if any."""
    return str((payload or {}).get(REF_LABEL_KEY) or "").strip()


def ref_label(refs: dict[str, tuple[str, str]], ref: str) -> str:
    """`button "Send invoice"` for a ref the last snapshot knew, else empty."""
    entry = refs.get(str(ref or ""))
    if not entry:
        return ""
    role, name = entry
    return f'{role} "{name}"' if name else role


#: The page a ref was read off, and the tab it belonged to. Recorded next to
#: `ref_label` and, like it, never sent to the sidecar — neither key is in any
#: `op.fields`, so `request_body` drops both.
#:
#: They exist for one reason: an approval is a decision about *an element on a
#: page*, and re-resolving `button "Delete account"` without checking which page
#: is in front of us would happily click a same-named button on whatever the tab
#: navigated to in the meantime. See `approved_target`.
REF_PAGE_KEY = "ref_url"
REF_TARGET_KEY = "ref_target"

#: Annotations the loop attaches for its own bookkeeping and a human reading the
#: step log does not want. `ref_label` is deliberately *not* one of them: it is
#: the description the whole gate is built on and belongs in every transcript.
#: These three are addresses, not descriptions.
QUIET_ANNOTATIONS: frozenset[str] = frozenset({REF_PAGE_KEY, REF_TARGET_KEY, "snapshot_id"})


def annotations_hidden(payload: dict[str, Any] | None) -> dict[str, Any]:
    """`payload` without the bookkeeping keys, for anything a person reads."""
    return {k: v for k, v in (payload or {}).items() if k not in QUIET_ANNOTATIONS}


# ---------------------------------------------------------------------------
# Re-deriving a reference from the element's recorded identity
# ---------------------------------------------------------------------------
#
# Two callers, one algorithm, two different sentences.
#
# **The approved path.** The gate holds `browser_click(ref='e9', snapshot_id='s3', ref_label='button
# "Delete account"')` and shows the human the *label*. By the time they press
# Approve — a minute, an hour, a day later — `s3` is long evicted and `e9` may
# be pointing at something else entirely, so replaying the payload verbatim
# earns a `409 stale_ref` essentially every time. That refusal is correct, and
# it is also useless: it turns "do the thing I approved" into "re-run the whole
# task", and an approval flow that reliably fails is one people route around.
#
# What the human approved was the *sentence*, not the node id. So execution
# re-derives the ref: take a fresh snapshot, find the element whose role and
# accessible name are the ones in the approval text, and act on that. The
# safety property is that this can only ever click the element the approval
# described or nothing at all —
#
#   * one match on the same page  -> act on it;
#   * no match                    -> `approved_element_missing`;
#   * more than one match         -> `approved_element_ambiguous`;
#   * a different page            -> `approved_page_changed`;
#   * a restarted browser         -> `browser_session_lost`;
#
# — and there is deliberately no positional fallback, no "closest match" and no
# `force`. Guessing between two Delete buttons is precisely the thing the gate
# exists to prevent, and a gate that guesses when it is unsure is not a gate.
#
# **The ordinary path.** Mid-task the same thing happens for a different reason.
# A real run: 33 desktop actions, 32 of which ran, and the one failure was
# `browser_click(ref='e514') — stale_ref — e514 belongs to snapshot s14, not
# s15`. The bot had looked at the page again between deciding on an element and
# acting on it. Nothing about the element had changed. The refusal is correct
# and it costs a step, a model call and the owner's money, every time, for as
# long as the model is expected to track ref lifetime by hand across a
# forty-step task. It will not do so reliably; no one would.
#
# So the same re-derivation runs there too, with two differences that matter:
#
#   * it is **reactive**, not proactive. An approval is hours old and its ref is
#     stale essentially always, so `_perform_approved_browser` re-resolves
#     before it tries anything. Mid-loop the ref is usually fine, so a
#     pre-emptive snapshot on every step would double the sidecar traffic to fix
#     a problem that mostly is not there. The ordinary path therefore tries the
#     call first and only re-resolves after the sidecar says `stale_ref` or
#     `unknown_ref` — the two codes that are *proof the sidecar did nothing*,
#     because both are raised by `resolve()` before any input is dispatched.
#     No other failure is retried: a `cdp_timeout` might have landed, and
#     retrying something that might have landed is how a bot sends twice.
#   * it says so. `resolve_approved` speaks about an approval a person read;
#     `resolve_recovered` speaks about a reference that went stale under the
#     model's feet, and names the same six honest outcomes in the model's own
#     vocabulary so it can act on them.
#
# `_resolve_identity` is the decision both share. It is a pure function of a
# target and a snapshot and has no opinion about which caller is asking, so the
# safety argument is made once and cannot drift between the two paths.

#: `button "Delete account"` -> `("button", "Delete account")`; `button` -> ("button", "").
_LABEL_RE = re.compile(r'^(\S+)(?:\s+"(.*)")?$')

#: How many elements the re-resolution snapshot asks for. The sidecar's ceiling.
#: Uniqueness is the thing being proved, so a truncated snapshot cannot prove it
#: and `resolve_approved` refuses rather than assuming the rest of the page held
#: nothing else with that name.
IDENTITY_SNAPSHOT_MAX_ELEMENTS = 1000

#: Synthesised by the API, not the sidecar, when an approved action could not be
#: re-resolved to exactly the element the approval named. `409` for the same
#: reason the sidecar uses it: *I refused rather than doing the wrong thing.*
APPROVED_ELEMENT_MISSING = "approved_element_missing"
APPROVED_ELEMENT_AMBIGUOUS = "approved_element_ambiguous"
APPROVED_PAGE_CHANGED = "approved_page_changed"
BROWSER_SESSION_LOST = "browser_session_lost"

#: The same three outcomes on the ordinary path, where there is no approval to
#: speak about — only a reference the page moved out from under. Distinct codes
#: rather than a re-used `stale_ref`, because "your ref went stale AND the
#: element it named is gone" and "your ref went stale AND there are now two of
#: them" have different next moves and a model told only `stale_ref` will
#: re-snapshot and try the same thing again.
REF_ELEMENT_MISSING = "ref_element_missing"
REF_ELEMENT_AMBIGUOUS = "ref_element_ambiguous"
REF_PAGE_CHANGED = "ref_page_changed"

#: The only two sidecar codes an ordinary DOM action is retried after.
#:
#: Both are raised by the sidecar's `resolve()` *before* it dispatches anything
#: to the page, so both are proof that nothing happened — which is what makes a
#: retry a first attempt rather than a second one. `cdp_timeout`, `cdp_error`
#: and a dropped connection are all cases where the click may well have landed,
#: and they are deliberately absent: a bot that retries those is a bot that
#: sends the message twice.
RECOVERABLE_REF_ERRORS: frozenset[str] = frozenset({"stale_ref", "unknown_ref"})

#: Set on a result that only succeeded because the reference was re-derived.
#: Never sent to the sidecar; it exists so `success_text` can tell the model its
#: ref was stale (so it stops reusing it) and so the step log does not claim a
#: plain success for something that took a recovery.
RECOVERED_KEY = "recovered_ref"


@dataclass(frozen=True)
class RefTarget:
    """The element identity a payload carries, recovered from its annotations."""

    action: str
    role: str
    name: str
    url: str
    target_id: str
    payload: dict[str, Any]

    @property
    def described(self) -> str:
        """Exactly the phrase the approval text showed the human."""
        return f'{self.role} "{self.name}"' if self.name else self.role


#: The old name, from when the approved path was the only caller. Kept so the
#: gate's own vocabulary reads the way its tests and its docstrings do.
ApprovedTarget = RefTarget


def parse_ref_label(label: str) -> tuple[str, str]:
    """`role, name` out of a `ref_label`. `("", "")` when it is not one."""
    match = _LABEL_RE.match(str(label or "").strip())
    if not match:
        return "", ""
    return match.group(1), (match.group(2) or "")


def ref_identity(action: str, payload: dict[str, Any] | None) -> RefTarget | None:
    """The element identity this payload records, or None if it records none.

    None is the honest answer in two cases, and both mean *there is nothing to
    re-derive from* — the approved path then runs the payload unchanged, and the
    ordinary path lets the sidecar's own refusal stand:

    * the op does not address an element at all (`browser_navigate`,
      `browser_key`) — there is no identity, and the arguments *are* the action;
    * the payload carries no `ref_label`, which means whoever built the step had
      no snapshot that knew this ref. Then all anyone ever said was "act on `e9`
      of snapshot `s3`". For an approval that is literally what the human read,
      so running exactly that — pinned `snapshot_id` and all — is the truthful
      execution of it. For an ordinary call there is no identity to check a
      replacement element against, and re-resolving without one would be the
      positional guess this whole module refuses to make.
    """
    op = op_for(action)
    if op is None or "ref" not in op.fields:
        return None
    body = dict(payload or {})
    if not str(body.get("ref") or ""):
        return None
    role, name = parse_ref_label(label_in(body))
    if not role:
        return None
    return RefTarget(
        action=action,
        role=role,
        name=name,
        url=str(body.get(REF_PAGE_KEY) or ""),
        target_id=str(body.get(REF_TARGET_KEY) or ""),
        payload=body,
    )


#: What `_perform_approved_browser` has always called it.
approved_target = ref_identity


def identity_snapshot_request(target: ApprovedTarget) -> dict[str, Any]:
    """The `browser_snapshot` body that will contain the approved element if it exists.

    `viewport_only` is false because "the page scrolled" must not read as "the
    element is gone", and `include_text` is false because static text lines have
    no refs and cost bytes. The filter is by *name* only, never by role: a
    `name_filter` is a substring match on the same accessible name the label was
    rendered from, so the result is guaranteed to be a superset of the exact
    matches and uniqueness stays provable. Role is then matched here. An element
    whose accessible name is empty has nothing to filter on, so that case pays
    for a role-filtered snapshot instead.
    """
    request: dict[str, Any] = {
        "viewport_only": False,
        "include_text": False,
        "max_elements": IDENTITY_SNAPSHOT_MAX_ELEMENTS,
    }
    if target.name:
        request["name_filter"] = target.name
    else:
        request["role_filter"] = target.role
    if target.target_id:
        request["target_id"] = target.target_id
    return request


def _refusal(action: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "action": action, "error": code, "status": 409, "detail": detail, **extra}


def _same_page(approved: str, current: str) -> bool:
    """Is `current` the page the approval was about?

    Scheme, host and path, ignoring query and fragment. A fragment is
    same-document by definition and a query is in-page state — a search results
    page that re-sorted itself is still the page the human was looking at, and
    refusing there would reintroduce the failure this whole path exists to fix.
    A different host or path is a different page, full stop.
    """
    if not approved:
        # Nothing was recorded (an approval held before this field existed).
        # Identity alone is what was approved, so identity alone is checked.
        return True
    return approved.split("?", 1)[0].split("#", 1)[0] == current.split("?", 1)[0].split("#", 1)[0]


#: The seven answers `_resolve_identity` can give. Named rather than returned as
#: strings at each site so the two renderers below cannot fall out of step with
#: it, and so adding an eighth is a change every caller has to acknowledge.
FOUND = "found"
LOOK_FAILED = "look_failed"
SESSION_LOST = "session_lost"
PAGE_CHANGED = "page_changed"
TRUNCATED = "truncated"
MISSING = "missing"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Resolution:
    """What a fresh snapshot says about the element a payload recorded.

    Facts only. Whether they are told as "the approval named a button that is
    gone" or as "your ref went stale and the button is gone" is the renderer's
    business, and there are two renderers because the two callers are answering
    different people.
    """

    outcome: str
    #: The ref the element has *now*, on `FOUND`.
    ref: str = ""
    #: The snapshot that minted it, so the retry can pin its own lookup.
    snapshot_id: str = ""
    #: Where the tab actually is. Empty when the look itself failed.
    current_url: str = ""
    #: Every ref that matched, on `AMBIGUOUS` — the model gets to see them.
    matches: tuple[str, ...] = ()


def _resolve_identity(target: RefTarget, snapshot: dict[str, Any]) -> Resolution:
    """Where the recorded element is now, decided once for both callers.

    Pure: a target and the result of `identity_snapshot_request(target)` in, a
    verdict out. The whole safety argument lives here, so it is made once —

    * **identity, never position.** The match is on role *and* the exact
      accessible name Chrome computed, so a replacement is the element that was
      described or it is not a match at all;
    * **the recorded page is checked too.** A same-named control on whatever the
      tab navigated to is a different control;
    * **uniqueness has to be provable.** Two matches is a refusal, and so is one
      match inside a snapshot that admits it was cut short, because a truncated
      snapshot cannot prove there is not a second one further down;
    * **no fallback of any kind.** No nearest match, no first match, no `force`.

    — and neither caller can weaken it without weakening both, which is the
    point of it being one function.
    """
    if not snapshot.get("ok"):
        return Resolution(LOOK_FAILED)

    current = str(snapshot.get("url") or "")
    if not _same_page(target.url, current):
        if current.startswith("about:blank"):
            return Resolution(SESSION_LOST, current_url=current)
        return Resolution(PAGE_CHANGED, current_url=current)

    refs = parse_snapshot_refs(str(snapshot.get("snapshot") or ""))
    matches = tuple(
        sorted(
            ref
            for ref, (role, name) in refs.items()
            if role == target.role and name == target.name
        )
    )
    partial = bool(snapshot.get("truncated")) or bool(snapshot.get("byte_capped"))

    if len(matches) == 1 and not partial:
        return Resolution(
            FOUND,
            ref=matches[0],
            snapshot_id=str(snapshot.get("snapshot_id") or ""),
            current_url=current,
            matches=matches,
        )
    if partial:
        # One match in a snapshot that admits it is incomplete is not a unique
        # match, and acting on it would be the positional guess in disguise.
        return Resolution(TRUNCATED, current_url=current, matches=matches)
    if not matches:
        return Resolution(MISSING, current_url=current)
    return Resolution(AMBIGUOUS, current_url=current, matches=matches)


def _retargeted(target: RefTarget, found: Resolution) -> dict[str, Any]:
    """The original payload, re-pointed at the element that is there now."""
    return {**target.payload, "ref": found.ref, "snapshot_id": found.snapshot_id}


def resolve_approved(target: ApprovedTarget, snapshot: dict[str, Any]) -> dict[str, Any]:
    """The payload to send, or the refusal to return. Never a guess.

    Takes the *result* of `identity_snapshot_request(target)` and answers with
    either `{"payload": {...}}` — the approved action re-pointed at the element
    the human named — or `{"failure": {...}}`, a full sidecar-shaped envelope
    `result_text` can render.

    Every sentence below is addressed to the person who pressed Approve and to
    the model that has to explain the outcome to them, which is why they are
    written here rather than shared with the ordinary path: "you approved
    `button "Delete account"` on this page and the tab is now on that one" is
    the only useful way to say `PAGE_CHANGED` to someone who is waiting on an
    approval, and it would be nonsense mid-loop.
    """
    verdict = _resolve_identity(target, snapshot)

    if verdict.outcome == FOUND:
        return {"payload": _retargeted(target, verdict)}

    if verdict.outcome == LOOK_FAILED:
        # Could not even look. Pass the sidecar's own diagnosis through rather
        # than inventing one on top of it, but say which action did not run.
        return {
            "failure": {
                **snapshot,
                "action": target.action,
                "detail": (
                    f"could not read the page to find {target.described} again, so the "
                    f"approved {target.action} did not run: "
                    + str(snapshot.get("detail") or snapshot.get("error") or "no detail given")
                ),
            }
        }

    current = verdict.current_url
    if verdict.outcome == SESSION_LOST:
        return {
            "failure": _refusal(
                target.action,
                BROWSER_SESSION_LOST,
                f"the tab is on {current or 'nothing'} — the browser was restarted after "
                f"you approved, so the page you approved {target.described} on is gone. "
                "Nothing was clicked.",
                approved_url=target.url,
                current_url=current,
            )
        }
    if verdict.outcome == PAGE_CHANGED:
        return {
            "failure": _refusal(
                target.action,
                APPROVED_PAGE_CHANGED,
                f"you approved {target.described} on {target.url}, and the tab is now on "
                f"{current}. Nothing was clicked.",
                approved_url=target.url,
                current_url=current,
            )
        }
    if verdict.outcome == TRUNCATED:
        return {
            "failure": _refusal(
                target.action,
                APPROVED_ELEMENT_AMBIGUOUS,
                f"the page has more elements than one snapshot can show, so I cannot prove "
                f"{target.described} is the only one. Nothing was clicked.",
                matched=len(verdict.matches),
            )
        }
    if verdict.outcome == MISSING:
        return {
            "failure": _refusal(
                target.action,
                APPROVED_ELEMENT_MISSING,
                f"nothing on {current} is {target.described} any more, so the approved "
                f"{target.action} had nothing to act on. Nothing was clicked.",
                current_url=current,
            )
        }
    return {
        "failure": _refusal(
            target.action,
            APPROVED_ELEMENT_AMBIGUOUS,
            f"{len(verdict.matches)} elements on {current} are now {target.described} "
            f"({', '.join(verdict.matches)}), and choosing between them is the guess "
            "this gate exists to prevent. Nothing was clicked.",
            matched=len(verdict.matches),
            current_url=current,
        )
    }


def resolve_recovered(
    target: RefTarget, snapshot: dict[str, Any], cause: dict[str, Any]
) -> dict[str, Any]:
    """The same verdict, said to the model whose reference just went stale.

    `cause` is the sidecar's own refusal — the `stale_ref` or `unknown_ref` that
    started this — and it is carried into every sentence, because "your click
    failed" and "your click failed *and here is what I tried about it*" are
    different amounts of information and the second one is what stops the model
    reaching for the same dead reference again.

    Answers `{"payload": ...}` or `{"failure": ...}` exactly as
    `resolve_approved` does, so `simulation` treats the two the same way.
    """
    verdict = _resolve_identity(target, snapshot)
    action, described = target.action, target.described
    ref = str(target.payload.get("ref") or "")
    code = str(cause.get("error") or "stale_ref")
    #: Every refusal below opens with this, so the model is never told about the
    #: recovery without also being told what it was recovering from.
    lead = f"{ref} was refused as {code}, so I looked again for {described}"

    if verdict.outcome == FOUND:
        return {"payload": _retargeted(target, verdict)}

    if verdict.outcome == LOOK_FAILED:
        return {
            "failure": {
                **snapshot,
                "action": action,
                "detail": (
                    f"{lead}, and could not read the page at all: "
                    + str(snapshot.get("detail") or snapshot.get("error") or "no detail given")
                ),
            }
        }

    current = verdict.current_url
    if verdict.outcome == SESSION_LOST:
        return {
            "failure": _refusal(
                action,
                BROWSER_SESSION_LOST,
                f"{lead}. The tab is on {current or 'nothing'} — the browser was restarted, "
                f"so the page {described} was on is gone along with anything you were signed "
                "in to. Nothing was clicked.",
                recorded_url=target.url,
                current_url=current,
                caused_by=code,
            )
        }
    if verdict.outcome == PAGE_CHANGED:
        return {
            "failure": _refusal(
                action,
                REF_PAGE_CHANGED,
                f"{lead}. You read that ref off {target.url} and the tab is now on {current}; "
                "a same-named control on a different page is a different control, so nothing "
                "was clicked.",
                recorded_url=target.url,
                current_url=current,
                caused_by=code,
            )
        }
    if verdict.outcome == TRUNCATED:
        return {
            "failure": _refusal(
                action,
                REF_ELEMENT_AMBIGUOUS,
                f"{lead}. The page has more elements than one snapshot can show, so I cannot "
                f"prove {described} is the only one and will not guess. Nothing was clicked.",
                matched=len(verdict.matches),
                current_url=current,
                caused_by=code,
            )
        }
    if verdict.outcome == MISSING:
        return {
            "failure": _refusal(
                action,
                REF_ELEMENT_MISSING,
                f"{lead}. Nothing on {current} is {described} any more. Nothing was clicked.",
                current_url=current,
                caused_by=code,
            )
        }
    return {
        "failure": _refusal(
            action,
            REF_ELEMENT_AMBIGUOUS,
            f"{lead}. {len(verdict.matches)} elements on {current} are now {described} "
            f"({', '.join(verdict.matches)}), and picking one of them would be a guess. "
            "Nothing was clicked.",
            matched=len(verdict.matches),
            current_url=current,
            caused_by=code,
        )
    }


# ---------------------------------------------------------------------------
# Turning a sidecar answer into a sentence
# ---------------------------------------------------------------------------

#: `error` code -> what to do about it. This is the whole point of surfacing
#: the sidecar's contract instead of flattening it to "the click failed": every
#: one of these is a state a real site produced against the sidecar lane, and
#: every one has a different correct next move. A model told only "failed"
#: retries the same click; a model told "a consent banner is on top of it"
#: dismisses the banner.
ERROR_GUIDANCE: dict[str, str] = {
    "stale_ref": (
        "The page changed under that reference. Take a fresh browser_snapshot and use "
        "the new refs — do not reuse the old one."
    ),
    "unknown_ref": (
        "That reference is not from a live snapshot. Take a browser_snapshot first and "
        "use a ref from it."
    ),
    "not_actionable": (
        "The element is hidden, disabled, or outside the viewport. Try browser_scroll "
        "with that ref to bring it into view, then re-snapshot. If it is disabled, "
        "something else on the page has to happen first."
    ),
    "obscured": (
        "Something is covering that element — the error names it, and on a real site it "
        "is usually a cookie or consent banner. Snapshot, dismiss the covering element, "
        "then come back to this one."
    ),
    "select_failed": (
        "That control is not a native <select>. If it is an ARIA combobox: browser_click "
        "the control, browser_snapshot again, then browser_click the option you want."
    ),
    "selector_not_found": "No element matched that CSS selector. Snapshot the page and check it.",
    "bad_selector": "That CSS selector is not valid. Snapshot the page and check it.",
    "missing_selector": "until=selector needs a selector.",
    "missing_text": "until=text needs text.",
    "unknown_target": "There is no tab with that target_id. Call browser_tabs for the live list.",
    "no_dialog": "No dialog is open on this tab, so there is nothing to answer.",
    "no_history_entry": "There is nowhere to go in that direction in this tab's history.",
    "url_not_allowed": (
        "This browser opens http(s):// URLs, about:blank and file:///home/nesq/ only."
    ),
    "unknown_key": "That key name is not one this browser accepts.",
    "navigation_failed": "The page did not load. Check the URL, or try again.",
    "cdp_error": "Chromium refused the command. Re-snapshot and try a different approach.",
    "cdp_timeout": (
        "Chromium did not answer in time. If a dialog is pending, clear it with "
        "browser_dialog first — a blocking alert() freezes the whole page."
    ),
    "wait_timeout": (
        "It did not happen before the timeout. The page may not be doing what you "
        "expect — snapshot and look."
    ),
    "browser_unavailable": (
        "DOM control is not available on this desktop right now. Fall back to the pixel "
        "tools: screenshot, then click/type at coordinates."
    ),
    "browser_not_supported": (
        "This desktop is running an image from before DOM browser control existed, so it "
        "has no /browser endpoints at all and no browser_* tool can work on it. Do not "
        "retry them. Use the pixel tools (screenshot, then click/type at coordinates), "
        "and tell the person their desktop needs stopping and starting again to pick up "
        "a current image."
    ),
    APPROVED_ELEMENT_MISSING: (
        "The element the approval named is not on the page any more, so nothing was done. "
        "Nothing else was clicked in its place. Take a fresh browser_snapshot and ask "
        "again if the task still needs doing."
    ),
    APPROVED_ELEMENT_AMBIGUOUS: (
        "More than one element now matches the description that was approved, and this "
        "will not guess between them. Nothing was done. Narrow the page down — scroll, "
        "filter, or open the specific record — snapshot again and ask again."
    ),
    APPROVED_PAGE_CHANGED: (
        "The tab has moved to a different page since the approval, and a same-named "
        "control on another page is a different control. Nothing was done. Navigate back "
        "to the page the task is about, snapshot, and ask again."
    ),
    BROWSER_SESSION_LOST: (
        # Reached from both the approved path and an ordinary stale ref, so it
        # says what is true of the browser rather than what is true of an
        # approval. The `detail` above it already names which of the two asked.
        "The browser was restarted, so its pages and its signed-in session are gone. "
        "Nothing was done. Start again from navigation, and expect to sign in again."
    ),
    REF_ELEMENT_MISSING: (
        "Your reference was stale and the element it named is not on the page any more, so "
        "nothing was done and nothing else was acted on in its place. Take a fresh "
        "browser_snapshot and look at what the page offers now."
    ),
    REF_ELEMENT_AMBIGUOUS: (
        "Your reference was stale and more than one element now answers to that description, "
        "so nothing was done rather than guessing between them. Narrow the page down — "
        "scroll, filter, or open the specific record — then browser_snapshot and act on a "
        "ref from that."
    ),
    REF_PAGE_CHANGED: (
        "Your reference was stale and the tab has moved to a different page since you read "
        "it, so nothing was done. If the task is still about the old page, navigate back to "
        "it; otherwise browser_snapshot where you are and work from that."
    ),
}

#: The sidecar has no `/browser/*` lane at all. Distinct from
#: `browser_unavailable` (Chromium is wedged on a desktop that *does* have the
#: lane) because the remedy is different and permanent: a long-running desktop
#: from before the DOM release answers `404` to every one of these paths, and a
#: bot that reads that as "the page failed" will keep asking. One real session
#: spent thirty-six steps guessing coordinates because a `404` arrived as a
#: failure with no code, no detail and no remedy attached to it.
BROWSER_NOT_SUPPORTED = "browser_not_supported"

#: The `error` value the proxy synthesises when it could not reach the sidecar
#: at all. Same code the sidecar itself uses for a wedged Chromium, because it
#: means the same thing to the caller: use pixels.
BROWSER_UNAVAILABLE = "browser_unavailable"

#: DOM control is not merely broken, it is absent, and no amount of retrying a
#: `browser_*` tool will change that. The agent loop degrades to pixels on
#: either of these rather than spending one of its lives on them.
BROWSER_ABSENT: frozenset[str] = frozenset({BROWSER_UNAVAILABLE, BROWSER_NOT_SUPPORTED})


def envelope(action: str, status: int, body: Any) -> dict[str, Any]:
    """One sidecar answer, guaranteed to carry a code when it is a failure.

    The proxy used to trust `ok` and `error` straight out of the body on the
    grounds that "the sidecar sets both and they always agree". True of a
    sidecar that has the `/browser/*` lane — and the whole point of a
    version-skewed deployment is that the thing answering might not be one. A
    desktop still running a pre-DOM image answers FastAPI's
    `404 {"detail": "Not Found"}`, which has neither key, and the result was a
    step logged as `browser_tabs() — failed — no reason given`: nothing for the
    model to recover from and nothing for the person to act on. In a product
    whose claim is that you can see what your bot did, that is the worst
    possible failure text.

    So: a body that already speaks the error contract passes through untouched,
    and anything else is classified here by status. Nothing that leaves this
    function can be a failure without a code and a sentence.
    """
    result: dict[str, Any] = {
        **(body if isinstance(body, dict) else {}),
        "action": action,
        "status": status,
    }
    if result.get("ok") or result.get("error"):
        return result

    # Say `ok: false` rather than leaving it absent. Every consumer reads it
    # with `.get`, so absent already means false — but a result that has to be
    # interpreted to be understood is how the missing `error` got through.
    result["ok"] = False
    detail = str(result.get("detail") or "").strip()
    if status in (404, 405):
        result["error"] = BROWSER_NOT_SUPPORTED
        result["detail"] = (
            f"the desktop sidecar answered {status} for this browser endpoint"
            + (f" ({detail})" if detail else "")
            + " — this desktop is running an image from before DOM browser control and has "
            "no /browser lane at all"
        )
    elif status >= 400:
        result["error"] = "cdp_error"
        result["detail"] = detail or (
            f"the desktop sidecar answered {status} with no error code"
        )
    else:
        # A 2xx that is not `{"ok": true, ...}` is not a success this can vouch
        # for, and reporting it as one is how a bot claims work it never did.
        result["error"] = "cdp_error"
        result["detail"] = detail or (
            f"the desktop sidecar answered {status} without the documented "
            "{ok: true, ...} envelope"
        )
    return result


def short_failure(result: dict[str, Any]) -> str:
    """One line naming why a browser call failed, for the step log a human reads.

    `error` alone is a code; `detail` alone has no vocabulary. The transcript
    gets both, because "failed — no reason given" is what this exists to stop.
    """
    code = str(result.get("error") or "unknown_error")
    status = result.get("status")
    detail = str(result.get("detail") or "").strip()
    head = f"{code} ({status})" if status else code
    return f"{head}: {detail}" if detail else head


#: The same failures as `ERROR_GUIDANCE`, said to the person who asked for the
#: work rather than to the model that has to recover from it.
#:
#: `short_failure` writes the debugging line — `stale_ref (409): e514 belongs to
#: snapshot s14, not s15` — which is exactly right for the audit trail and
#: exactly wrong in a reply, where `ref`, `409` and `s14` name nothing the
#: reader has ever seen. These carry the same fact in words that survive
#: leaving this repo.
#:
#: Two properties are load-bearing, and neither is negotiable for the sake of a
#: nicer sentence. **Every one of them still says the action did not happen** —
#: a translated failure is a failure, and softening one into "I had a look at
#: the page" is the same lie as reporting a mock CRM row. And **a code with no
#: entry here gets no sentence**, so the caller falls back to `short_failure`
#: rather than to a comfortable guess: an unrecognised failure has to read as
#: unrecognised, never as fine.
PLAIN_FAILURES: dict[str, str] = {
    "stale_ref": "the page had changed by the time I went to act on it",
    "unknown_ref": "the page had changed by the time I went to act on it",
    "not_actionable": "the control was hidden, greyed out, or off the bottom of the page",
    "obscured": "something was covering it, which on a real site is usually a cookie banner",
    "select_failed": "that dropdown is not a normal one and did not take the choice",
    "selector_not_found": "nothing on the page matched what I was looking for",
    "bad_selector": "what I was looking for was not a valid thing to look for",
    "missing_selector": "I did not say what to wait for",
    "missing_text": "I did not say what text to wait for",
    "unknown_target": "that tab was not open any more",
    "no_dialog": "there was no browser prompt open to answer",
    "no_history_entry": "there was nowhere to go in that direction",
    "url_not_allowed": "I am not allowed to open that kind of address",
    "unknown_key": "that is not a key this browser accepts",
    "navigation_failed": "the page did not load",
    "cdp_error": "the browser refused to do it",
    "cdp_timeout": "the browser stopped answering",
    "wait_timeout": "it still had not happened by the time I gave up waiting",
    BROWSER_UNAVAILABLE: (
        "the browser was not answering, so I had to work from a picture of the screen instead"
    ),
    BROWSER_NOT_SUPPORTED: (
        "this desktop is running an older image that cannot be driven through the browser "
        "at all, and stopping and starting it would pick up a current one"
    ),
    APPROVED_ELEMENT_MISSING: (
        "by the time you approved it, the control it named was gone from the page, so "
        "nothing was clicked in its place"
    ),
    APPROVED_ELEMENT_AMBIGUOUS: (
        "by the time you approved it, more than one thing on the page matched, and I will "
        "not guess between them"
    ),
    APPROVED_PAGE_CHANGED: "the page had moved on from the one you approved it against",
    BROWSER_SESSION_LOST: (
        "the browser had been restarted, so the page you approved it against was gone"
    ),
}


def plain_failure(result: dict[str, Any]) -> str:
    """Why a browser call did not land, for the person who asked for the work.

    Empty when `PLAIN_FAILURES` has no sentence for the code, which is the
    signal to the caller to print the technical line instead.
    """
    return PLAIN_FAILURES.get(str(result.get("error") or ""), "")


def _clip(text: str, limit: int = RESULT_MAX_CHARS) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 40] + f"\n… [{len(text) - limit + 40} more characters not shown]"


def _refs_are_void() -> str:
    return (
        "Every element reference from before this call is now void — take a "
        "browser_snapshot before acting."
    )


def failure_text(action: str, result: dict[str, Any]) -> str:
    """What the model reads when a browser call did not succeed.

    Carries three things and never fewer: the machine code, the sidecar's own
    detail, and the remedy. Flattening any of them is how `409 obscured` — "I
    refused rather than clicking the wrong thing" — becomes indistinguishable
    from a broken page, and a model that cannot tell those apart retries.
    """
    code = str(result.get("error") or "unknown_error")
    status = result.get("status")
    detail = str(result.get("detail") or "").strip()
    head = f"{action} FAILED ({status} {code})" if status else f"{action} FAILED ({code})"
    lines = [f"{head}: {detail}" if detail else f"{head}."]

    expected, actual = result.get("expected"), result.get("actual")
    if isinstance(expected, dict) and isinstance(actual, dict):
        lines.append(
            f"That ref was {expected.get('role')} \"{expected.get('name')}\" and is now "
            f"{actual.get('role')} \"{actual.get('name')}\"."
        )
    options = result.get("options")
    if isinstance(options, list) and options:
        lines.append("The real options are: " + _clip(json.dumps(options), 1200) + ".")
    if result.get("pending_dialog"):
        lines.append(
            "A javascript dialog is open and blocking the page: "
            + _clip(json.dumps(result["pending_dialog"]), 400)
            + ". Answer it with browser_dialog before anything else."
        )
    guidance = ERROR_GUIDANCE.get(code) or str(result.get("hint") or "")
    if guidance:
        lines.append(guidance)
    lines.append("Nothing on the page changed as a result of this call.")
    return " ".join(line for line in lines if line)


def _rows_line(result: dict[str, Any]) -> str:
    total = result.get("interactive_total")
    matched, returned = result.get("matched"), result.get("returned")
    bits: list[str] = []
    if returned is not None and total is not None:
        bits.append(f"showing {returned} of {total} interactive elements")
    if matched is not None and matched != returned:
        bits.append(f"{matched} matched the filters")
    if result.get("truncated"):
        bits.append("raise max_elements, or narrow with name_filter / role_filter, to see more")
    if result.get("byte_capped"):
        bits.append("the rendering hit its byte cap and was cut short")
    return "; ".join(bits)


def success_text(action: str, result: dict[str, Any]) -> str:  # noqa: C901,PLR0912 - one table, read down
    """What the model reads when a browser call worked.

    Every branch renders values that came back from the sidecar. Nothing here
    describes a page the model was not shown, and nothing claims an action had
    an effect the response does not report — the two failures the loop's own
    docstring exists to prevent, restated at the DOM boundary.
    """
    lines: list[str] = []

    if action == "browser_snapshot":
        head = (
            f"browser_snapshot of \"{result.get('title', '')}\" — {result.get('url', '')} "
            f"(snapshot_id={result.get('snapshot_id')})."
        )
        counts = _rows_line(result)
        if counts:
            head += f" {counts}."
        if result.get("frames", 1) and int(result.get("frames") or 1) > 1:
            head += (
                f" {result['frames']} frames were included; an iframe that loads late is "
                "only attached by the next snapshot, so snapshot twice if a widget is missing."
            )
        lines.append(head)
        body = result.get("snapshot")
        if body is None and result.get("elements") is not None:
            body = json.dumps(result["elements"])
        lines.append(_clip(body or "(the page exposed no interactive elements)"))

    elif action in ("browser_navigate", "browser_back", "browser_forward", "browser_reload"):
        lines.append(
            f"{action} ran. The tab is now on \"{result.get('title', '')}\" — "
            f"{result.get('url', '')}"
            + (f" ({result['load_state']})." if result.get("load_state") else ".")
        )
        lines.append(_refs_are_void())

    elif action in ("browser_click", "browser_hover"):
        target = f"{result.get('role', '')} \"{result.get('name', '')}\"".strip()
        lines.append(f"{action} ran on {target or 'the element'}.")
        if result.get("new_tabs"):
            lines.append(
                f"It opened {len(result['new_tabs'])} new tab(s): {result['new_tabs']}. "
                "Use browser_tabs / browser_tab_activate to work in one."
            )
        if result.get("switched_to_tab"):
            lines.append(f"The tab it lives in was brought forward ({result['switched_to_tab']}).")

    elif action == "browser_type":
        target = f"{result.get('role', '')} \"{result.get('name', '')}\"".strip()
        lines.append(
            f"browser_type put {result.get('chars', '?')} character(s) into "
            f"{target or 'the field'}."
        )
        if result.get("submitted"):
            lines.append("Enter was pressed afterwards, so the form may have been submitted.")

    elif action == "browser_select":
        lines.append(f"browser_select chose {json.dumps(result.get('selected') or [])}.")

    elif action == "browser_scroll":
        if result.get("scrolled_into_view"):
            lines.append(f"browser_scroll brought {result.get('ref')} into view.")
        else:
            lines.append(
                f"browser_scroll moved the page to y={result.get('y')} of {result.get('maxY')}."
            )
        lines.append("Positions moved, so re-snapshot before acting on a ref you had.")

    elif action == "browser_key":
        lines.append(f"browser_key pressed {result.get('key')}.")

    elif action == "browser_text":
        lines.append(
            f"browser_text read {result.get('length', '?')} characters from "
            f"\"{result.get('title', '')}\""
            + (" (truncated)." if result.get("truncated") else ".")
        )
        lines.append(_clip(str(result.get("text") or "")))

    elif action == "browser_extract":
        lines.append(
            f"browser_extract returned {result.get('returned', 0)} of "
            f"{result.get('total', 0)} rows"
            + (" (truncated — raise limit)." if result.get("truncated") else ".")
        )
        lines.append(_clip(json.dumps(result.get("rows") or [], ensure_ascii=False)))

    elif action == "browser_wait":
        lines.append(
            f"browser_wait: {result.get('until')} ({result.get('state')}) happened after "
            f"{result.get('waited_ms')}ms."
        )

    elif action in ("browser_tabs", "browser_status"):
        tabs = result.get("tabs") or []
        lines.append(f"{len(tabs)} tab(s) open; the active one is {result.get('active_target')}.")
        lines.append(_clip(json.dumps(tabs, ensure_ascii=False), 4000))

    elif action == "browser_tab_new":
        lines.append(
            f"browser_tab_new opened {result.get('url')} as tab {result.get('target_id')}."
        )

    elif action == "browser_tab_activate":
        lines.append(
            f"browser_tab_activate switched to \"{result.get('title', '')}\" — "
            f"{result.get('url', '')}."
        )
        lines.append(_refs_are_void())

    elif action == "browser_tab_close":
        lines.append(f"browser_tab_close closed {result.get('closed')}.")

    elif action == "browser_dialog":
        lines.append(
            f"browser_dialog answered the dialog: {json.dumps(result.get('handled') or {})}. "
            "The page is unblocked."
        )

    else:  # pragma: no cover - defensive; every op in the table is above
        lines.append(f"{action} ran and reported success.")

    # A ref that had to be re-derived is still a success, and saying so would be
    # enough — except that the model is holding a whole snapshot of references
    # that are just as dead as the one it used, and will reach for the next one.
    # Two sentences here save several failed steps later.
    if result.get(RECOVERED_KEY):
        lines.append(
            f"Note: the ref you gave was no longer valid ({result.get('recovered_from')}), so "
            f"I found {result.get('recovered_label') or 'that element'} again on the same page "
            f"and acted on it as {result.get('ref') or 'a fresh ref'}. Every other ref you are "
            "holding is just as stale — browser_snapshot before your next one."
        )

    # A click that opens `alert()`/`confirm()` freezes the renderer, so the
    # sidecar answers `ok: true` with the dialog attached: the click *landed*
    # and retrying it would double-fire whatever it did. The model has to be
    # told both halves of that or it will retry.
    if result.get("pending_dialog"):
        lines.append(
            "IMPORTANT: this call landed AND opened a javascript dialog which is now "
            "blocking the page: "
            + _clip(json.dumps(result["pending_dialog"]), 400)
            + ". Do NOT repeat the call — it already happened. Answer the dialog with "
            "browser_dialog, then carry on."
        )
    return "\n".join(line for line in lines if line)


def result_text(action: str, result: dict[str, Any]) -> str:
    """The one entry point the loop and the router both use."""
    if not result.get("ok"):
        return failure_text(action, result)
    return success_text(action, result)
