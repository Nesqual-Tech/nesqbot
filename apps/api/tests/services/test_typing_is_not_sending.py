"""Writing into a field is not transmitting; submitting is.

Reported from production. A model declared `send` on typing a message into
LinkedIn's composer, so one act cost the person two approvals — one to write the
words, another to press Send — and each stopped the run and waited. The words go
nowhere until something submits them.

This is the only place a declared risk is capped rather than obeyed. The reason
is narrow and worth stating: escalate-only exists so a model can protect the user
with something the server cannot see. Here the server sees *more* than the model,
because it is holding the arguments. `submit` is a fact; "this feels like
sending" is a guess.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _assess(db, bot, user, action, args, declared=None):
    from app.services import simulation
    from app.services.simulation import Effect

    return await simulation._assess_desktop(
        db,
        Effect(
            kind="desktop",
            bot_id=bot.id,
            actor_user_id=user.id,
            action=action,
            input_data=args,
            declared_risk=declared,
        ),
    )


async def test_typing_without_submit_is_not_a_send(db, user_a, bot_a):
    """The reported bug: the composer asked for approval before the Send button did."""
    assessed = await _assess(
        db,
        bot_a,
        user_a,
        "browser_type",
        {"ref": "e358", "ref_label": 'textbox "Message..."', "text": "Salut!"},
        declared="send",
    )
    assert assessed.risk != "send", "typing into a box was treated as transmitting"
    assert not assessed.requires_approval, "the person was asked to approve writing words"


async def test_typing_that_submits_still_stops(db, user_a, bot_a):
    """`submit=True` presses Enter, which on a composer does send. That must gate."""
    assessed = await _assess(
        db,
        bot_a,
        user_a,
        "browser_type",
        {"ref": "e358", "ref_label": 'textbox "Message..."', "text": "Salut!", "submit": True},
        declared="send",
    )
    assert assessed.risk == "send"
    assert assessed.requires_approval


async def test_the_cap_is_only_ever_applied_to_typing(db, user_a, bot_a):
    """A declared `send` on a click is still obeyed — the cap is not a loophole."""
    assessed = await _assess(
        db,
        bot_a,
        user_a,
        "browser_click",
        {"ref": "e9", "ref_label": 'button "Something"'},
        declared="send",
    )
    assert assessed.risk == "send"
    assert assessed.requires_approval


async def test_a_dangerous_label_still_wins_over_the_cap(db, user_a, bot_a):
    """The cap touches the *declaration*, never the label classifier.

    If the element being typed into is named for a destructive act, the label
    still escalates — the cap must not become a way to reach one.
    """
    assessed = await _assess(
        db,
        bot_a,
        user_a,
        "browser_type",
        {"ref": "e9", "ref_label": 'textbox "Delete account"', "text": "DELETE"},
        declared="send",
    )
    assert assessed.risk == "delete"
    assert assessed.requires_approval
