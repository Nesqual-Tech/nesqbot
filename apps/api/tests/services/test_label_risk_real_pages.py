"""Label classification, against labels taken from real runs.

Every `observe` case here parked a production run waiting for a human to approve
something harmless: opening a login form, following a hashtag, clicking a
person's name. Each stop cost the task, because a gated step does not queue — it
stops the work and waits.

Every `send`/`spend`/`delete` case is a control that must still stop.

The rule these encode: **a link navigates, a button acts.** That is the role
Chrome computed, not a guess about English, and it is what separates
`link "Sign in with email"` from `button "Send"` when both contain a keyword.
`delete` and `spend` still escalate on a link, because those are the two classes
where being wrong is expensive rather than merely annoying.
"""

from __future__ import annotations

import pytest

from app.services.risk import classify_label_risk

# Seen in production, every one of them a false positive that stopped a run.
HARMLESS = [
    'link "Sign in with email"',
    'link "Hashtag #cabinetstomatologic 27.9K posts"',
    'link "Paula H."',
    'link "Contact info"',
    'link "Message"',
    'link "Webbplats"',
    'link "Programare"',
    'button "Accept"',
    'button "Dismiss"',
    'button "Compose message"',
    'button "Star Dental Clinic by Medfactory"',
    'link "1,204 shares"',
    'link "Shared with you"',
    'link "Posts"',
]

DANGEROUS = [
    ('button "Send"', "send"),
    ('button "Send message"', "send"),
    ('button "Post"', "send"),
    ('button "Share"', "send"),
    ('button "Delete account"', "delete"),
    ('link "Delete account"', "delete"),
    ('button "Buy now"', "spend"),
    ('link "Buy now"', "spend"),
    ('button "Email invoice"', "spend"),
    ('button "Pay"', "spend"),
]


@pytest.mark.parametrize("label", HARMLESS)
def test_navigation_does_not_ask_for_approval(label):
    assert classify_label_risk(label) == "observe", (
        f"{label} would park the run waiting for a human with nothing to approve"
    )


@pytest.mark.parametrize(("label", "expected"), DANGEROUS)
def test_the_controls_that_matter_still_stop(label, expected):
    assert classify_label_risk(label) == expected


def test_an_unrecognised_label_shape_keeps_full_sensitivity():
    """A bare string is not `role "name"`, so it is read whole — the safe default."""
    assert classify_label_risk("Send") == "send"
    assert classify_label_risk("Delete everything") == "delete"


def test_a_link_is_not_a_way_around_the_expensive_classes():
    """The link rule relaxes `send` only. Money and destruction still stop."""
    assert classify_label_risk('link "Delete account"') == "delete"
    assert classify_label_risk('link "Purchase"') == "spend"
