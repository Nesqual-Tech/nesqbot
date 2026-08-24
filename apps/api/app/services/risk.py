"""The risk vocabulary and the one name-based classifier the whole app shares.

This used to live in `services/desktop.py`, which was accurate while desktop
steps were the only thing classified by name. They are not any more: MCP tool
calls are gated by the same rule, and a classifier that lives in a
device-specific module invites a second copy for every new caller. A gate that
exists on one execution path and not another is not a gate — so the vocabulary
moved here and `services.desktop` re-exports it. There is exactly one
implementation, and this is it.

Three callers, one rule:

* `services.desktop` / `routers.desktop` — desktop primitives;
* `services.simulation._assess_desktop` — the same, at the chokepoint;
* `services.simulation._assess_mcp` — MCP tool names.

The DOM browser surface is the same desktop by another interface, so its risks
are merged in from `services.browser` rather than restated: a `browser_*` name
classifies here, once, like everything else.

Connector actions are *not* classified from their name: their manifest declares
a risk, which is authoritative. Everywhere, a declared risk may only **raise**
the classification, never lower it — see `max_risk`.

`classify_label_risk` is the one thing here that classifies something other
than a name. It reads the *accessible name of the element* a DOM action is
about to touch, which is a question the pixel lane could never ask.
"""

from __future__ import annotations

import re

from app.services.browser import BROWSER_ACTION_RISKS

#: Risk vocabulary ordered least to most dangerous. Escalation compares rank,
#: so which classification wins is explicit rather than an artefact of the
#: order the checks happen to run in.
RISK_ORDER: tuple[str, ...] = ("observe", "draft", "mutate", "send", "spend", "delete")
RISK_RANK: dict[str, int] = {risk: rank for rank, risk in enumerate(RISK_ORDER)}

#: Unrecognised actions are assumed to change something.
DEFAULT_ACTION_RISK = "mutate"

#: Structural table of *desktop primitives*: the ones that only observe or move
#: a cursor are safe to run unattended; anything that can leak data or reach
#: outside the box is a mutate. Names not listed here fall to
#: `DEFAULT_ACTION_RISK`, which is why an MCP tool nobody has heard of starts at
#: `mutate` rather than at `observe`.
ACTION_RISKS: dict[str, str] = {
    "click": "observe",
    "type": "observe",
    "key": "observe",
    "mousemove": "observe",
    "screenshot": "observe",
    "open_chromium": "observe",
    "clipboard_set": "mutate",
    # Pure reads of the desktop. They fell through to the `mutate` default,
    # which is safe (nothing gates on mutate) but wrong in the audit trail:
    # an operator reading the log should not see "the bot mutated something"
    # when it listed windows or scrolled a page. The default stays `mutate`
    # precisely so an *unrecognised* action is never assumed harmless - these
    # are recognised, and they observe.
    "windows": "observe",
    "scroll": "observe",
    # Handing work to another bot. Listed rather than left to the `mutate`
    # default so the name is *recognised*: the audit row for a delegation reads
    # "mutate" because this table says so, not because nobody had heard of the
    # action. `mutate` and no higher on purpose — nothing gates on mutate, and
    # putting a human approval in front of every hand-off would break the one
    # thing delegation exists to do. What the receiving bot then goes on to do
    # is classified here step by step, exactly as it would be had a person
    # asked it directly.
    "delegate_to_bot": "mutate",
    # The DOM half of the same desktop. Merged from `services.browser` rather
    # than restated, so the table the tools are generated from and the table
    # the gate classifies against are one table. Without this every
    # `browser_*` name would fall to the `mutate` default and the audit log
    # would say a bot mutated something when it read a page.
    **BROWSER_ACTION_RISKS,
}

#: Substring -> risk escalation on the action *name*. A taught routine or a
#: third-party MCP server can name a step anything, so the structural table
#: above cannot be exhaustive: these catch "send_invoice", "delete_draft" and
#: friends.
RISK_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("delete", "remove", "erase", "wipe", "drop", "trash", "destroy"), "delete"),
    (
        ("pay", "purchase", "buy", "checkout", "order", "spend", "transfer", "charge", "invoice"),
        "spend",
    ),
    (("send", "submit", "post", "publish", "reply", "email", "share"), "send"),
)


def risk_rank(risk: str) -> int:
    """Severity of a risk label; unknown labels sort as the default."""
    return RISK_RANK.get(risk, RISK_RANK[DEFAULT_ACTION_RISK])


def max_risk(*risks: str) -> str:
    """The most dangerous of the given risk labels.

    This is what makes every declared risk escalate-only: a step, a manifest or
    an MCP server can raise its own classification but never lower it.
    """
    return max(risks, key=risk_rank)


def classify_action_risk(action: str) -> str:
    """Risk class for an action known only by name — the single source of truth.

    Two readings are combined and the *more dangerous* one always wins:

    1. structural: an exact match in ``ACTION_RISKS`` (``click`` -> observe,
       ``clipboard_set`` -> mutate, ...), defaulting to ``mutate``;
    2. keyword: a substring hit in ``RISK_KEYWORDS`` escalating to
       send / spend / delete.

    Escalation is by explicit rank, never by which branch runs first, so
    ``send_invoice`` resolves to the higher of {send, spend} and a structurally
    "observe" action can never stay observe once a keyword matches.
    """
    name = (action or "").strip().lower()
    risk = ACTION_RISKS.get(name, DEFAULT_ACTION_RISK)
    for keywords, escalation in RISK_KEYWORDS:
        if any(keyword in name for keyword in keywords):
            risk = max_risk(risk, escalation)
    return risk


#: Word forms a label keyword is allowed to appear in: the bare word, and
#: nothing else.
#:
#: Whole-word matching already kept "Postcode" out of the `send` bucket. The
#: inflections did the opposite of their intent — they turned **nouns back into
#: verbs**, and every social page is built from those nouns:
#:
#:     'Hashtag #cabinetstomatologic 27.9K posts'  -> send    ("posts")
#:     '1,204 shares'                              -> send    ("shares")
#:     'Shared with you'                           -> send    ("shared")
#:     'Order history'                             -> spend   ("order")
#:
#: A lead-generation run was parked for approval on the first of those: a
#: *hashtag link*. The person approved it, and by then the model had moved on —
#: so the cost of a false positive here is not one extra click, it is the task
#: stopping.
#:
#: A control that performs the act is labelled with the bare imperative — Send,
#: Post, Share, Order, Buy, Delete. A count, a heading or a folder uses the
#: plural or the past tense. Matching only the bare word keeps every real
#: control and drops the nouns. Where an inflected label really is dangerous the
#: keywords overlap and catch it anyway: "Send invoices" is still `send`.
_LABEL_SUFFIXES = ("",)
_WORD_RE = re.compile(r"[a-z]+")


def classify_label_risk(label: str) -> str:
    """Risk implied by the *thing being acted on*, from its accessible name.

    This is the one classification a DOM lane can make and a pixel lane cannot.
    A `click` is named for the motion, so `services.desktop` has never had any
    idea whether it lands on a scrollbar or on Send, and the only defence was
    asking the model to declare it. A `/browser/click` names an element whose
    accessible name was computed by Chrome, so the server can read "Delete
    account" and hold the step for a human whether or not the model said
    anything. See `simulation._assess_desktop`, which is where it is applied,
    and `browser.REF_LABEL_KEY`, which is how the name gets there.

    Two deliberate differences from `classify_action_risk`:

    * it starts at ``observe``, not at ``mutate``. An action name is a
      vocabulary and an unrecognised one deserves suspicion; a label is free
      text and "Learn more" means nothing.
    * it matches whole words. ``"post" in "postcode"`` is true and useless.

    Escalate-only, like everything else here: it can raise what a step
    classifies as and never lower it, so the worst a mis-read name can do is
    put a click in front of a human.
    """
    role, name = _split_role(label or "")
    words = set(_WORD_RE.findall(name.lower()))
    if not words:
        return "observe"
    risk = "observe"
    for keywords, escalation in RISK_KEYWORDS:
        if role == "link" and escalation == "send":
            # A link navigates; a button acts. That is not a guess about English,
            # it is what the role means in the accessibility contract Chrome
            # computed — and it is the difference between reading a page and
            # changing the world.
            #
            # Without this, `link "Sign in with email"` classified as `send`,
            # because "email" is a send keyword, and every LinkedIn login parked
            # the run waiting for a human to approve *opening a login form*. The
            # noun-shaped labels that reach us are overwhelmingly links: sign-in
            # links, hashtag links, "27.9K posts", "Share" counts.
            #
            # `delete` and `spend` still escalate on a link, because a link
            # labelled "Delete account" or "Buy now" is worth stopping for even
            # if the markup is unusual, and because those are the two classes
            # where being wrong is expensive rather than merely annoying.
            continue
        forms = {keyword + suffix for keyword in keywords for suffix in _LABEL_SUFFIXES}
        if words & forms:
            risk = max_risk(risk, escalation)
    return risk


#: `browser_ops` renders a target as `role "accessible name"`. Both halves matter
#: and they matter differently, so they are separated before either is read.
_ROLE_RE = re.compile(r'^\s*([a-z]+)\s+"(.*)"\s*$', re.DOTALL)


def _split_role(label: str) -> tuple[str, str]:
    """`button "Send"` -> `("button", "Send")`; anything else -> `("", label)`.

    A label that does not match the shape is classified whole, which is the
    conservative reading: an unrecognised format keeps full sensitivity rather
    than quietly losing it.
    """
    match = _ROLE_RE.match(label)
    if not match:
        return "", label
    return match.group(1).lower(), match.group(2)
