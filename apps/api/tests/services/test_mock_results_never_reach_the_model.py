"""Mock connector output must never enter the model's context as a finding.

Production regression, and the second time this product invented work.

`_gather_tools` speculatively calls connectors before the model turn. With no
`base_url` bound, `crm.search_accounts` returns a canned row built from the
query itself - `Acme (<whatever the user just typed>)`. That was dumped into the
prompt notes verbatim, and the Lead Generator duly told the user:

    I found 1 account in the CRM search result:
    - Acme (I need you to do something You will connect to linkedin and start
      searching and ) - Stage: Qualified

The row carried `"mock": true` and the model reported it anyway. A label is not
a safeguard when the fabricated payload is sitting right next to it, so the
payload is withheld instead.

The same absence is also useful: told plainly that it has no CRM, the bot is
pointed at the browser it does have.
"""

from __future__ import annotations

import pytest

from app.services import orchestrator as orch


def _notes_for(results, notes):
    return "\n".join(notes)


@pytest.fixture
def mock_result():
    return {
        "ok": True,
        "mock": True,
        "connector": "crm",
        "action": "search_accounts",
        "result": [{"id": "acc_1", "name": "Acme (whatever the user typed)", "stage": "Qualified"}],
    }


def test_the_fabricated_payload_is_not_in_the_note(mock_result):
    """The specific string that reached a user must not be constructible."""
    note = orch._mock_context_note("crm", "crm")
    assert "Acme" not in note
    assert "Qualified" not in note
    assert "acc_1" not in note


def test_the_note_says_there_is_no_connection_and_no_data(mock_result):
    note = orch._mock_context_note("crm", "crm").lower()
    assert "no live crm connection" in note
    assert "no data" in note


def test_the_note_forbids_reporting_records(mock_result):
    note = orch._mock_context_note("crm", "crm").lower()
    assert "do not report" in note


def test_the_note_points_at_the_desktop(mock_result):
    """The honest absence should redirect, not just refuse."""
    assert "desktop" in orch._mock_context_note("crm", "crm").lower()
