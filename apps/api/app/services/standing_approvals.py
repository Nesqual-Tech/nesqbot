"""Standing approvals — *"don't ask again for this button"*, made auditable.

The owner approved a click and typed **"don't ask again for this button"** into
the note field of approval `5ad46fc5`. Nothing read it. The next identical click
asked again. This module is what reads it, and what makes reading it defensible.

The design is not in question — the product owner chose it, including the part I
argued with. What is in question, every time, is whether a bot quietly acquiring
a standing permission can be explained to somebody afterwards. So the trigger is
as the owner asked (automatic **and** on request), and every safeguard that makes
it explicable is structural rather than conventional.

**What a rule is.** One human, one bot, one action, one control, one page.
`browser_click` on `button "Message"` on `linkedin.com/in/andrei-pop`. Not
`button "Message"` — the page is half the key, and it is compared the way
`browser._same_page` compares, on scheme+host+path. A page that renders an
attacker-chosen button with a whitelisted name is a different page and does not
match. Never expires; it ends when it is revoked.

**How a rule comes to exist.** Two ways, both of which end in a human's explicit
yes:

* `origin="note"` — they wrote it down. `note_text` keeps the exact words,
  because a paraphrase of the sentence that granted a permission is not
  evidence of anything.
* `origin="repetition"` — they said yes to the identical thing
  `REPETITION_THRESHOLD` times running. See that constant for why three.

**Five things that can never happen**, each enforced where it cannot be argued
with rather than where it reads nicely:

1. *A rule with no traceable origin.* A database CHECK refuses the row. There is
   no code path that writes one, and if somebody writes one there is still no
   row.
2. *A `spend` or `delete` rule.* A second CHECK refuses those outright;
   `LEARNABLE_RISKS` is narrower still. Money and destruction ask every time,
   and `MONEY_AND_DESTRUCTION_ALWAYS_ASK` is the sentence the UI shows so the
   limit is visible rather than discovered.
3. *Learning from anything but a human's explicit yes.* `learn_from_decision`
   requires `status == "approved"`, a real `decided_by`, and an execution that
   actually ran. A rejection, an expiry or a step that never landed teaches
   nothing — otherwise rules bootstrap themselves, which is the whole failure
   mode.
4. *A rule applying to somebody else.* `owner_user_id` is part of the lookup
   key, and an effect with no `actor_user_id` matches nothing.
5. *A rule surviving revocation.* `revoked_at` is part of the partial unique
   index the lookup reads, so a revoked rule stops matching on the next query
   and there is no cache to invalidate.

**Where it is applied.** At the gate, in `simulation.perform`, as a recorded
reason the gate did not stop — never as a second path around it. The effect then
goes down the *approved* execution path, so the element is re-derived from its
recorded identity against a fresh snapshot and the action refuses rather than
guesses when the page changed, the element is gone, two now match, or the
snapshot was truncated. An unattended send gets a stricter proof of what it is
about to touch than an attended one, which is the correct way round.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Approval, AuditEvent, StandingApproval
from app.services import browser as browser_ops

if TYPE_CHECKING:  # pragma: no cover - typing only; importing it for real would cycle
    from app.services.simulation import Effect

logger = logging.getLogger(__name__)

#: The risk classes a standing permission may ever cover.
#:
#: `send` and nothing else. It is the class the owner is drowning in — one
#: "Message" button per lead, forty leads a morning — and it is the class where
#: the cost of asking again is the task stopping rather than a wasted click.
#:
#: `mutate`, `draft` and `observe` are absent because they are meaningless here:
#: `connectors.requires_approval` gates exactly send/spend/delete, so nothing
#: below `send` ever reaches a gate for a rule to open.
LEARNABLE_RISKS: frozenset[str] = frozenset({"send"})

#: The two that keep asking, every time, forever.
#:
#: A database CHECK enforces this independently of the constant above, because
#: "we only learn `send`" is a policy and a later change could widen it by
#: accident, whereas "money and destruction are never learned" is the promise.
NEVER_LEARNED: frozenset[str] = frozenset({"spend", "delete"})

#: Said in the UI, next to the list, rather than left to be discovered.
#:
#: A limit a person finds out about by being asked again when they thought they
#: had said "stop asking" reads as a bug. The same limit stated up front reads as
#: the product having a spine.
MONEY_AND_DESTRUCTION_ALWAYS_ASK = (
    "Anything that spends money or deletes something always asks, every time. "
    "That cannot be switched off."
)

#: How many identical yeses become a standing permission.
#:
#: **Three, and the defence is the shape of the alternatives.**
#:
#: *One* is consent to an instance. The person looked at one click, on one page,
#: and allowed it; reading a policy into that is inventing an intention they did
#: not express.
#:
#: *Two* is a coincidence. Two is what one task done twice looks like — the same
#: lead opened again, a retry after a failure, a run resumed. There is no moment
#: at which a second yes is evidence the first was about a pattern rather than
#: about a situation.
#:
#: *Three* is the smallest number that is a habit. Three separate approvals of
#: the identical control on the identical page, by the same person, with no
#: refusal and no expiry in between (`_unbroken_run` enforces the "in between"),
#: is the first point at which "they keep saying yes to this" describes
#: behaviour rather than guessing at it.
#:
#: Higher would be safer and is the wrong trade: the owner is asking for this
#: because the asking is what costs them, and a threshold high enough never to
#: fire is a feature that does not exist. The real safety is not the number — it
#: is that the first auto-creation is *announced* in the reply, listed, and
#: revocable in one action. No number makes silent acquisition acceptable, and
#: announcement is what makes three acceptable.
REPETITION_THRESHOLD = 3

#: How far back `_recent_decisions` looks for identical approvals.
#:
#: The window is over *decided approvals for this control on this page*, which is
#: a handful of rows even for a heavy user, so this is generous rather than
#: tuned. It exists so the query is bounded, not to express a policy.
_HISTORY_WINDOW = 25

#: Ways of writing "stop asking me about this".
#:
#: Deliberately a short, literal list of imperatives rather than anything
#: clever. A false positive here silently grants a standing permission on the
#: strength of a sentence that did not ask for one, which is the single worst
#: outcome this module can produce — worse than missing a phrasing, because a
#: missed phrasing costs one more click and the person can say it again.
#:
#: Romanian is included because the owner is Romanian and writes to the bot in
#: both languages. The bot's outbound copy is Romanian; its notes are not
#: reliably either.
_ASKED_TO_STOP: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdon'?t\s+ask\s+(?:me\s+)?again\b"),
    re.compile(r"\bdo\s+not\s+ask\s+(?:me\s+)?again\b"),
    re.compile(r"\bstop\s+asking\b"),
    re.compile(r"\bno\s+need\s+to\s+ask\s+(?:me\s+)?again\b"),
    re.compile(r"\balways\s+(?:allow|approve)\b"),
    re.compile(r"\bnu\s+m[ăa]\s+mai\s+[îi]ntreba\b"),
    re.compile(r"\bnu\s+mai\s+[îi]ntreba\b"),
)

#: Where the note lands when `approvals.note` is not a column in this build.
#: `routers.approvals.decide_approval` writes one or the other; both are read.
_NOTE_IN_PAYLOAD = "decision_note"


# ---------------------------------------------------------------------------
# The identity a rule is about
# ---------------------------------------------------------------------------


def url_key(raw: Any) -> str:
    """`https://www.linkedin.com/in/andrei-pop` out of that page's real URL.

    Scheme, host and path; query and fragment dropped. Exactly the comparison
    `browser._same_page` makes, and for exactly its reasons: a fragment is
    same-document by definition and a query is in-page state, so a results page
    that re-sorted itself is still the page the human was looking at. A
    different host or path is a different page, full stop — which is what stops
    a whitelisted name on an attacker's page matching a rule.

    Empty for anything that is not http(s). `about:blank` and `file:///home/nesq/`
    are both reachable through the browser lane and neither has a host to bind a
    grant to; a rule keyed on one of them would be keyed on nothing.
    """
    url = str(raw or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    return url.split("?", 1)[0].split("#", 1)[0]


@dataclass(frozen=True)
class Identity:
    """The one control on the one page a rule can be about.

    Built only by `identity_of`, which refuses every incomplete case, so a value
    of this type is a promise that all four fields are usable as a lookup key.
    """

    action: str
    role: str
    name: str
    url_key: str

    @property
    def described(self) -> str:
        """`button "Message"` — the phrase the human read on the approval."""
        return f'{self.role} "{self.name}"'


def identity_of(action: str, payload: dict[str, Any] | None) -> Identity | None:
    """The identity this step records, or None when it records no usable one.

    None in four cases, and every one of them means *there is nothing here that
    could safely be matched again later*:

    * the action is not one whose **target** is classified. `BROWSER_TARGETED`
      is the three ops that commit something, and it is the same set
      `simulation._assess_desktop` escalates on. A rule over `browser_scroll`
      would be a permission to move the page;
    * no `ref_label`, so nobody recorded what the element was. All that was ever
      said is "act on `e9`", and `e9` names nothing tomorrow;
    * an empty accessible name. "This button" has to be a button somebody can
      point at; matching on role alone would cover every button on the page;
    * no http(s) page recorded. See `url_key`.
    """
    if action not in browser_ops.BROWSER_TARGETED:
        return None
    target = browser_ops.ref_identity(action, payload)
    if target is None or not target.name.strip() or not target.role.strip():
        return None
    key = url_key(target.url)
    if not key:
        return None
    return Identity(action=action, role=target.role, name=target.name, url_key=key)


def _single_step(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """The one held desktop step, or None if the approval is not exactly one.

    A multi-step approval is a decision about a *sequence*, and "don't ask again
    for this button" is not a sentence about a sequence. Nothing is learned from
    one, which also means the held-steps replay lane cannot be used to smuggle a
    grant in behind one recognisable step.
    """
    body = payload or {}
    if body.get("kind") != "desktop_steps":
        return None
    steps = body.get("steps") or []
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        return None
    return steps[0]


def approval_identity(approval: Approval) -> Identity | None:
    """The identity a held approval is about, or None."""
    step = _single_step(approval.payload)
    if step is None:
        return None
    action = str(step.get("action") or "")
    return identity_of(action, {k: v for k, v in step.items() if k != "action"})


def decision_note(approval: Approval) -> str:
    """The note the person typed when they decided, from wherever it landed."""
    column = getattr(approval, "note", None)
    if column:
        return str(column)
    return str((approval.payload or {}).get(_NOTE_IN_PAYLOAD) or "")


def asks_to_stop_asking(note: str) -> bool:
    """Did this note ask, in words, not to be asked again?

    Conservative on purpose — see `_ASKED_TO_STOP`. A note that means it and is
    not recognised costs one more click; a note that does not mean it and *is*
    recognised costs a standing permission nobody granted.
    """
    text = " ".join(str(note or "").lower().split())
    return any(pattern.search(text) for pattern in _ASKED_TO_STOP)


# ---------------------------------------------------------------------------
# Applying a rule — read by the gate, on every held-risk desktop step
# ---------------------------------------------------------------------------


async def covering(db: AsyncSession, *, effect: Effect, risk: str) -> StandingApproval | None:
    """The live rule that lets this effect through the gate, or None.

    Called from `simulation.perform` at the moment the gate would otherwise stop,
    and nowhere else: a standing approval that could be consulted from a second
    place would be a second gate.

    Every early return is a refusal to guess. No actor, no rule — an effect with
    no human behind it has nobody's consent to inherit. Not a `send`, no rule —
    `spend` and `delete` keep asking whatever is in the table. No recorded
    identity, no rule — see `identity_of`.
    """
    if effect.kind != "desktop" or effect.actor_user_id is None:
        return None
    if risk in NEVER_LEARNED or risk not in LEARNABLE_RISKS:
        return None
    identity = identity_of(effect.action, effect.input_data)
    if identity is None:
        return None

    rows = await db.execute(
        select(StandingApproval).where(
            StandingApproval.owner_user_id == effect.actor_user_id,
            StandingApproval.bot_id == effect.bot_id,
            StandingApproval.action == identity.action,
            StandingApproval.url_key == identity.url_key,
            StandingApproval.ref_role == identity.role,
            StandingApproval.ref_name == identity.name,
            StandingApproval.revoked_at.is_(None),
        )
    )
    matches = rows.scalars().all()
    if len(matches) != 1:
        # Zero is the ordinary answer. Two is impossible — the partial unique
        # index over the live rows says so — and if that index is ever gone this
        # refuses rather than picking one, which is the same discipline
        # `browser._resolve_identity` applies to two matching elements.
        if matches:
            logger.error(
                "%d live standing approvals match %s for bot %s; refusing to choose",
                len(matches),
                identity.described,
                effect.bot_id,
            )
        return None
    rule = matches[0]
    if rule.risk not in LEARNABLE_RISKS or rule.risk in NEVER_LEARNED:
        # A row whose grade is no longer coverable — reclassified, or written by
        # an older build. A grant does not survive its own risk class changing.
        return None
    return rule


def gate_note(rule: StandingApproval, described: str) -> str:
    """Why the gate did not stop, recorded on the assessment.

    This lands in `Assessment.notes`, which a rehearsal prints and `_plan_call`
    stores, so a dry run over a page with a standing permission says out loud
    that a `send` will go unattended and on whose authority. A gate that
    silently does not fire is indistinguishable from no gate.
    """
    granted = rule.created_at.date().isoformat() if rule.created_at else "an earlier date"
    origin = (
        f"you asked in writing ({rule.note_text.strip()!r})"
        if rule.origin == "note"
        else f"you approved it {len(rule.source_approval_ids or [])} times running"
    )
    return (
        f"a standing permission covers {described} on {rule.url_key}: {origin}, granted "
        f"{granted}. This step is not held for a decision; it is still classified "
        f"'{rule.risk}', still recorded, and the permission can be revoked from "
        "Standing permissions."
    )


async def record_use(
    db: AsyncSession, rule: StandingApproval, *, effect: Effect, outcome: dict[str, Any]
) -> None:
    """Stamp a use of a standing permission on the row and in the audit trail.

    Flushed, not committed: `simulation.perform`'s caller owns the transaction,
    exactly as `undo.record_effect` does. Never raises — a permission that ran
    and could not be counted is a bookkeeping loss, and failing the work that
    already happened in order to report it would be worse.
    """
    try:
        rule.use_count = int(rule.use_count or 0) + 1
        rule.last_used_at = datetime.now(timezone.utc)
        db.add(
            AuditEvent(
                actor_user_id=rule.owner_user_id,
                bot_id=effect.bot_id,
                event_type="standing_approval_applied",
                detail={
                    "standing_approval_id": str(rule.id),
                    "action": effect.action,
                    "risk": rule.risk,
                    "element": f'{rule.ref_role} "{rule.ref_name}"',
                    "url": rule.url_key,
                    "origin": rule.origin,
                    "run_id": str(effect.run_id) if effect.run_id else None,
                    "ok": bool(outcome.get("ok")),
                    "error": outcome.get("error"),
                    "use_count": rule.use_count,
                },
            )
        )
        await db.flush()
    except Exception as exc:  # noqa: BLE001 - the send already happened
        logger.warning("could not record use of standing approval %s: %s", rule.id, exc)


# ---------------------------------------------------------------------------
# Learning a rule — read by the decide endpoint, once, after the action ran
# ---------------------------------------------------------------------------


async def _recent_decisions(
    db: AsyncSession, approval: Approval, identity: Identity, owner_user_id: uuid.UUID
) -> list[Approval]:
    """Decided approvals about this identical control, newest first.

    The cheap, exact halves of the identity — kind, action, `ref_label` — are
    matched in SQL. The page is matched here, because `url_key` normalises the
    query string away and a JSONB comparison cannot.

    The candidate set is *this person's* history: approvals they decided, plus
    expired ones, which have no decider at all and are counted as theirs because
    the conservative reading of an unattributable expiry is that it breaks their
    run rather than somebody else's.
    """
    label = identity.described
    rows = await db.execute(
        select(Approval)
        .where(
            Approval.bot_id == approval.bot_id,
            Approval.status.in_(("approved", "rejected", "expired")),
            Approval.payload["kind"].astext == "desktop_steps",
            Approval.payload["steps"][0]["action"].astext == identity.action,
            Approval.payload["steps"][0][browser_ops.REF_LABEL_KEY].astext == label,
        )
        .order_by(Approval.created_at.desc(), Approval.id.desc())
        .limit(_HISTORY_WINDOW)
    )
    mine: list[Approval] = []
    for row in rows.scalars().all():
        if row.decided_by is not None and row.decided_by != owner_user_id:
            continue
        if row.decided_by is None and row.status != "expired":
            continue
        step = _single_step(row.payload)
        if step is None:
            continue
        if url_key(step.get(browser_ops.REF_PAGE_KEY)) != identity.url_key:
            continue
        mine.append(row)
    return mine


def _unbroken_run(history: list[Approval], owner_user_id: uuid.UUID) -> list[Approval]:
    """The newest `REPETITION_THRESHOLD` decisions, only if every one is a yes.

    "They approved it three times" and "they approved it three times, having
    refused it in between" are different facts and only the first is consent to a
    pattern. A refusal or an expiry anywhere in the window is evidence that the
    permission is situational, so the run is broken and counting starts again.

    Returns the run oldest-first when there is one, and an empty list otherwise.
    """
    window = history[:REPETITION_THRESHOLD]
    if len(window) < REPETITION_THRESHOLD:
        return []
    if any(a.status != "approved" or a.decided_by != owner_user_id for a in window):
        return []
    return list(reversed(window))


async def learn_from_decision(
    db: AsyncSession,
    approval: Approval,
    *,
    decided_by: uuid.UUID,
    execution: dict[str, Any] | None,
) -> StandingApproval | None:
    """Create the standing permission this decision earns, or None.

    Called once, from `routers.approvals.decide_approval`, after the held action
    has actually run and before the transaction commits — so the rule and the
    decision that created it land together or not at all.

    The gauntlet, in order, and every step of it is a reason a rule does not get
    created:

    * the decision has to be **approved**, by a **real human**, and the held
      action has to have **actually run**. An approved click that refused itself
      — the element was gone, two matched, the tab had navigated — teaches
      nothing, because the one thing a rule needs in order to be safe is proof
      that the identity resolves uniquely on that page, and a refusal is exactly
      the statement that it could not be proved;
    * it has to be **one desktop step** with a **complete identity**;
    * its risk has to be **learnable**. `spend` and `delete` stop here, and the
      database stops them again below;
    * and then either they **asked**, or they have said yes
      `REPETITION_THRESHOLD` times **running**.

    A rule that already exists is returned as None rather than re-created: the
    announcement is for the turn a permission is *acquired*, and announcing an
    existing one every time would train the reader to skip the sentence.
    """
    if approval.status != "approved" or approval.decided_by is None:
        return None
    if approval.decided_by != decided_by:  # pragma: no cover - the router passes its own
        return None
    if not (execution or {}).get("ok"):
        return None

    identity = approval_identity(approval)
    if identity is None:
        return None
    risk = str(approval.risk or "")
    if risk in NEVER_LEARNED or risk not in LEARNABLE_RISKS:
        return None

    existing = await db.execute(
        select(StandingApproval.id).where(
            StandingApproval.owner_user_id == decided_by,
            StandingApproval.bot_id == approval.bot_id,
            StandingApproval.action == identity.action,
            StandingApproval.url_key == identity.url_key,
            StandingApproval.ref_role == identity.role,
            StandingApproval.ref_name == identity.name,
            StandingApproval.revoked_at.is_(None),
        )
    )
    if existing.scalar_one_or_none() is not None:
        return None

    note = decision_note(approval).strip()
    if asks_to_stop_asking(note):
        origin, sources, note_text = "note", [approval], note
    else:
        history = await _recent_decisions(db, approval, identity, decided_by)
        run = _unbroken_run(history, decided_by)
        if not run:
            return None
        origin, sources, note_text = "repetition", run, ""

    rule = StandingApproval(
        owner_user_id=decided_by,
        bot_id=approval.bot_id,
        action=identity.action,
        risk=risk,
        ref_role=identity.role,
        ref_name=identity.name,
        url_key=identity.url_key,
        origin=origin,
        note_text=note_text,
        source_approval_ids=[str(a.id) for a in sources],
    )
    db.add(rule)
    db.add(
        AuditEvent(
            actor_user_id=decided_by,
            bot_id=approval.bot_id,
            event_type="standing_approval_granted",
            detail={
                "standing_approval_id": str(rule.id),
                "action": identity.action,
                "risk": risk,
                "element": identity.described,
                "url": identity.url_key,
                "origin": origin,
                "note": note_text,
                "source_approval_ids": [str(a.id) for a in sources],
            },
        )
    )
    try:
        await db.flush()
    except Exception as exc:  # noqa: BLE001 - a CHECK refusing the row is the point
        logger.warning("standing approval not created for approval %s: %s", approval.id, exc)
        return None
    return rule


# ---------------------------------------------------------------------------
# Listing and revoking
# ---------------------------------------------------------------------------


async def list_for_user(
    db: AsyncSession, owner_user_id: uuid.UUID, *, include_revoked: bool = False
) -> list[StandingApproval]:
    """Every permission this person has granted, newest first."""
    stmt = select(StandingApproval).where(StandingApproval.owner_user_id == owner_user_id)
    if not include_revoked:
        stmt = stmt.where(StandingApproval.revoked_at.is_(None))
    stmt = stmt.order_by(StandingApproval.created_at.desc())
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


async def revoke(
    db: AsyncSession, rule: StandingApproval, *, actor_user_id: uuid.UUID
) -> StandingApproval:
    """Take a permission back. One call, effective on the next gate decision.

    A timestamp, never a DELETE. The row is the record that the permission
    existed and was used `use_count` times, and that record outlives the
    permission — "what was this bot allowed to do in March" is a question with an
    answer.

    Idempotent: revoking a revoked rule keeps the first revocation, because the
    moment it stopped applying is a fact and the second press is not a new one.
    """
    if rule.revoked_at is None:
        rule.revoked_at = datetime.now(timezone.utc)
        rule.revoked_by = actor_user_id
        db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                bot_id=rule.bot_id,
                event_type="standing_approval_revoked",
                detail={
                    "standing_approval_id": str(rule.id),
                    "action": rule.action,
                    "element": f'{rule.ref_role} "{rule.ref_name}"',
                    "url": rule.url_key,
                    "used": int(rule.use_count or 0),
                },
            )
        )
        await db.flush()
    return rule
