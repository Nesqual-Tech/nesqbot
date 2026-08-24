"""`app.services.vendors` — the real call path, and the leak guarantee on it.

Two halves:

* **Behaviour.** Every driver is exercised through `httpx.MockTransport`: the
  request it builds, the normalisation it applies, and the error envelope it
  returns when the vendor is unreachable, refuses, or breaks. No live network,
  no real credentials.
* **The audit.** A static check over `app/services/vendors/` and the
  credential-handling services (see `AUDITED_SERVICES`) asserting that no
  secret-bearing name reaches a
  `return`, a logger, a `print`, or a `raise`. The runtime leak tests below
  cover what static analysis cannot: what a vendor echoes back, and what an
  `httpx` error carries.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from tenacity import stop_after_attempt, wait_exponential, wait_none

from app.services.vendors import base, crm, generic_http, graph, select_driver, ticketing

SENTINEL = "tok-must-never-be-echoed-4f81cd"

APP = Path(__file__).resolve().parents[2] / "app"
VENDORS_DIR = APP / "services" / "vendors"


def settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "graph_api_base_url": "https://graph.test/v1.0",
        "request_timeout_seconds": 7.5,
        "connector_live_calls": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Recorder:
    """Captures the requests a driver builds and answers each one."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self._responder(request)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def request(self) -> httpx.Request:
        assert self.requests, "the driver made no request"
        return self.requests[0]

    def json_body(self, index: int = 0) -> Any:
        import json

        return json.loads(self.requests[index].content.decode())


def transport_for(responder) -> tuple[httpx.MockTransport, Recorder]:
    recorder = Recorder(responder)
    return httpx.MockTransport(recorder), recorder


def json_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None):
    return lambda request: httpx.Response(status, json=payload, headers=headers or {})


async def run(driver, action, input_data, *, transport, credential=SENTINEL, manifest=None, **kw):
    return await driver.invoke(
        connector_id=kw.pop("connector_id", getattr(driver, "name", "x")),
        action=action,
        input_data=input_data,
        credential=credential,
        manifest=manifest or {},
        settings=kw.pop("settings", settings()),
        transport=transport,
    )


@pytest.fixture(autouse=True)
def _no_default_transport():
    """Nothing here may fall through to a real network."""
    base.set_default_transport(None)
    yield
    base.set_default_transport(None)


@pytest.fixture
def instant_retries(monkeypatch):
    """Keep the real retry predicate and attempt count; drop the backoff sleep."""
    from tenacity import AsyncRetrying, retry_if_exception

    monkeypatch.setattr(
        base,
        "_retrying",
        lambda: AsyncRetrying(
            stop=stop_after_attempt(base.RETRY_ATTEMPTS),
            wait=wait_none(),
            retry=retry_if_exception(base.is_retryable),
            reraise=True,
        ),
    )


# ---------------------------------------------------------------------------
# Driver selection
# ---------------------------------------------------------------------------


def test_first_party_connectors_get_their_own_driver():
    assert select_driver("microsoft_graph") is graph.driver
    assert select_driver("crm") is crm.driver
    assert select_driver("ticketing") is ticketing.driver


def test_an_unregistered_connector_is_manifest_driven():
    assert select_driver("invoice_portal") is generic_http.driver


# ---------------------------------------------------------------------------
# Microsoft Graph — list_inbox
# ---------------------------------------------------------------------------

GRAPH_MESSAGES = {
    "value": [
        {
            "id": "AAMkAGI1",
            "subject": "Invoice #4421",
            "bodyPreview": "Please find attached...",
            "from": {"emailAddress": {"name": "Vendor", "address": "vendor@example.com"}},
        },
        {
            "id": "AAMkAGI2",
            "subject": "Pricing question",
            "bodyPreview": "Can you send a quote?",
            "from": {"emailAddress": {"name": "Lead", "address": "lead@acme.com"}},
        },
    ]
}


async def test_list_inbox_builds_the_documented_graph_request():
    transport, recorder = transport_for(json_response(GRAPH_MESSAGES))
    await run(graph.driver, "list_inbox", {"top": 5}, transport=transport)

    request = recorder.request
    assert request.method == "GET"
    assert str(request.url).startswith("https://graph.test/v1.0/me/messages?")
    assert request.url.params["$top"] == "5"
    assert "bodyPreview" in request.url.params["$select"]
    assert request.headers["Authorization"] == f"Bearer {SENTINEL}"


async def test_list_inbox_normalises_onto_the_mock_shape():
    transport, _ = transport_for(json_response(GRAPH_MESSAGES))
    outcome = await run(graph.driver, "list_inbox", {"top": 2}, transport=transport)

    assert outcome.ok is True and outcome.live is True
    assert outcome.result == [
        {
            "id": "AAMkAGI1",
            "from": "vendor@example.com",
            "subject": "Invoice #4421",
            "snippet": "Please find attached...",
        },
        {
            "id": "AAMkAGI2",
            "from": "lead@acme.com",
            "subject": "Pricing question",
            "snippet": "Can you send a quote?",
        },
    ]


async def test_list_inbox_tolerates_a_message_with_no_sender_or_preview():
    transport, _ = transport_for(json_response({"value": [{"id": "m9"}]}))
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)
    assert outcome.result == [{"id": "m9", "from": "", "subject": "", "snippet": ""}]


async def test_an_empty_mailbox_normalises_to_an_empty_list():
    transport, _ = transport_for(json_response({"value": []}))
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)
    assert outcome.result == []


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [({}, "10"), ({"top": None}, "10"), ({"top": "abc"}, "10"), ({"top": 999}, "50"), ({"top": 0}, "1")],
)
async def test_list_inbox_clamps_top_to_something_graph_will_accept(supplied, expected):
    transport, recorder = transport_for(json_response({"value": []}))
    await run(graph.driver, "list_inbox", supplied, transport=transport)
    assert recorder.request.url.params["$top"] == expected


# ---------------------------------------------------------------------------
# Microsoft Graph — draft_reply
# ---------------------------------------------------------------------------


async def test_draft_reply_posts_create_reply_with_the_comment():
    transport, recorder = transport_for(json_response({"id": "AAMkDRAFT", "isDraft": True}, 201))
    outcome = await run(
        graph.driver, "draft_reply", {"message_id": "AAMkAGI1", "body": "on it"}, transport=transport
    )

    assert recorder.request.method == "POST"
    assert recorder.request.url.path == "/v1.0/me/messages/AAMkAGI1/createReply"
    assert recorder.json_body() == {"comment": "on it"}
    assert outcome.result == {
        "draft_id": "AAMkDRAFT",
        "message_id": "AAMkAGI1",
        "body": "on it",
        "state": "draft",
        "sent": False,
    }


async def test_draft_reply_url_quotes_a_graph_message_id():
    """Real Graph ids are base64url and can carry `/` and `=`."""
    transport, recorder = transport_for(json_response({"id": "d1"}, 201))
    await run(
        graph.driver,
        "draft_reply",
        {"message_id": "AAMk/AGI=1", "body": "hi"},
        transport=transport,
    )
    assert "AAMk%2FAGI%3D1" in str(recorder.request.url)


async def test_draft_reply_falls_back_to_a_local_draft_id_when_graph_returns_none():
    transport, _ = transport_for(lambda request: httpx.Response(201))
    outcome = await run(
        graph.driver, "draft_reply", {"message_id": "m1", "body": "hi"}, transport=transport
    )
    assert outcome.result["draft_id"] == "draft_m1"
    assert outcome.result["sent"] is False


# ---------------------------------------------------------------------------
# Microsoft Graph — send_mail
# ---------------------------------------------------------------------------


async def test_send_mail_builds_the_graph_message_envelope():
    transport, recorder = transport_for(
        lambda request: httpx.Response(202, headers={"request-id": "req-77"})
    )
    outcome = await run(
        graph.driver,
        "send_mail",
        {"to": "anna@acme.com", "subject": "Quote", "body": "Attached."},
        transport=transport,
    )

    assert recorder.request.method == "POST"
    assert recorder.request.url.path == "/v1.0/me/sendMail"
    assert recorder.json_body() == {
        "message": {
            "subject": "Quote",
            "body": {"contentType": "Text", "content": "Attached."},
            "toRecipients": [{"emailAddress": {"address": "anna@acme.com"}}],
        },
        "saveToSentItems": True,
    }
    assert outcome.result == {
        "message_id": "req-77",
        "to": "anna@acme.com",
        "subject": "Quote",
        "state": "sent",
        "sent": True,
    }


async def test_send_mail_splits_several_recipients():
    transport, recorder = transport_for(lambda request: httpx.Response(202))
    await run(
        graph.driver,
        "send_mail",
        {"to": "a@b.c, d@e.f; g@h.i", "subject": "s", "body": "b"},
        transport=transport,
    )
    addresses = [r["emailAddress"]["address"] for r in recorder.json_body()["message"]["toRecipients"]]
    assert addresses == ["a@b.c", "d@e.f", "g@h.i"]


async def test_send_mail_invents_a_message_id_when_graph_sends_no_request_id():
    transport, _ = transport_for(lambda request: httpx.Response(202))
    outcome = await run(
        graph.driver, "send_mail", {"to": "a@b.c", "subject": "s", "body": "b"}, transport=transport
    )
    assert outcome.result["message_id"].startswith("sent_")
    assert outcome.result["sent"] is True


# ---------------------------------------------------------------------------
# Microsoft Graph — configuration
# ---------------------------------------------------------------------------


def test_graph_is_not_configured_without_a_base_url():
    assert graph.driver.configured({}, settings(graph_api_base_url="")) is False
    assert graph.driver.configured({}, settings()) is True


def test_a_manifest_base_url_overrides_the_setting():
    assert graph._base_url({"base_url": "https://graph.microsoft.us/v1.0/"}, settings()) == (
        "https://graph.microsoft.us/v1.0"
    )


@pytest.mark.parametrize("action", ["list_inbox", "draft_reply", "send_mail"])
def test_graph_supports_its_three_actions(action):
    assert graph.driver.supports(action, {}) is True


def test_graph_does_not_claim_actions_it_has_not_implemented():
    assert graph.driver.supports("list_events", {}) is False


async def test_an_unsupported_graph_action_is_reported_not_raised():
    transport, _ = transport_for(json_response({}))
    outcome = await run(graph.driver, "list_events", {}, transport=transport)
    assert outcome.ok is False
    assert "no live implementation" in outcome.error


# ---------------------------------------------------------------------------
# generic_http — manifest substitution
# ---------------------------------------------------------------------------

PORTAL = {
    "id": "invoice_portal",
    "auth": "api_key",
    "api_key_header": "X-Api-Key",
    "base_url": "https://invoices.internal/api/",
    "actions": [
        {
            "name": "list_unpaid",
            "method": "GET",
            "path": "/invoices",
            "query": {"older_than": "{older_than_days}", "state": "unpaid"},
        },
        {
            "name": "draft_reminder",
            "method": "POST",
            "path": "/invoices/{invoice_id}/reminders",
            "body": {"note": "Reminder for {invoice_id}", "fields": "{fields}", "silent": False},
        },
        {"name": "mocked_only", "description": "no path, so no live call"},
    ],
}


async def test_generic_substitutes_path_and_query_from_validated_input():
    transport, recorder = transport_for(json_response([{"id": "inv_1"}]))
    outcome = await run(
        generic_http.driver,
        "list_unpaid",
        {"older_than_days": 30},
        transport=transport,
        manifest=PORTAL,
        connector_id="invoice_portal",
    )

    assert recorder.request.method == "GET"
    assert recorder.request.url.path == "/api/invoices"
    assert dict(recorder.request.url.params) == {"older_than": "30", "state": "unpaid"}
    assert outcome.result == [{"id": "inv_1"}]


async def test_generic_substitutes_the_path_and_the_body_template():
    transport, recorder = transport_for(json_response({"ok": 1}, 201))
    await run(
        generic_http.driver,
        "draft_reminder",
        {"invoice_id": "inv 42", "fields": {"stage": "late"}},
        transport=transport,
        manifest=PORTAL,
        connector_id="invoice_portal",
    )

    # `.path` decodes; the wire form is what the substitution has to get right.
    assert recorder.request.url.raw_path.decode() == "/api/invoices/inv%2042/reminders"
    assert recorder.json_body() == {
        "note": "Reminder for inv 42",
        # A value that is exactly one placeholder keeps the input's own type.
        "fields": {"stage": "late"},
        "silent": False,
    }


async def test_a_template_referencing_an_input_that_was_not_supplied_is_an_envelope_not_a_crash():
    transport, recorder = transport_for(json_response({}))
    outcome = await run(
        generic_http.driver,
        "draft_reminder",
        {"invoice_id": "inv_1"},
        transport=transport,
        manifest=PORTAL,
        connector_id="invoice_portal",
    )
    assert outcome.ok is False
    assert "fields" in outcome.error
    assert recorder.requests == [], "nothing should have been sent"


async def test_a_non_json_reply_is_wrapped_so_callers_always_get_a_container():
    transport, _ = transport_for(lambda request: httpx.Response(200, text="OK"))
    outcome = await run(
        generic_http.driver,
        "list_unpaid",
        {"older_than_days": 1},
        transport=transport,
        manifest=PORTAL,
        connector_id="invoice_portal",
    )
    assert outcome.result == {"status": 200, "body": "OK"}


# ---------------------------------------------------------------------------
# generic_http — auth per the manifest's `auth` field
# ---------------------------------------------------------------------------


async def test_api_key_auth_uses_the_manifest_header():
    transport, recorder = transport_for(json_response([]))
    await run(
        generic_http.driver,
        "list_unpaid",
        {"older_than_days": 1},
        transport=transport,
        manifest=PORTAL,
        connector_id="invoice_portal",
    )
    assert recorder.request.headers["X-Api-Key"] == SENTINEL
    assert "Authorization" not in recorder.request.headers


async def test_api_key_auth_without_a_named_header_uses_the_default():
    manifest = {**PORTAL, "api_key_header": None}
    transport, recorder = transport_for(json_response([]))
    await run(
        generic_http.driver,
        "list_unpaid",
        {"older_than_days": 1},
        transport=transport,
        manifest=manifest,
        connector_id="invoice_portal",
    )
    assert recorder.request.headers[base.DEFAULT_API_KEY_HEADER] == SENTINEL


async def test_oauth2_auth_sends_a_bearer_token():
    manifest = {**PORTAL, "auth": "oauth2"}
    transport, recorder = transport_for(json_response([]))
    await run(
        generic_http.driver,
        "list_unpaid",
        {"older_than_days": 1},
        transport=transport,
        manifest=manifest,
        connector_id="invoice_portal",
    )
    assert recorder.request.headers["Authorization"] == f"Bearer {SENTINEL}"
    assert "X-Api-Key" not in recorder.request.headers


@pytest.mark.parametrize("auth", ["none", "unrecognised", None])
async def test_auth_none_sends_no_credential_at_all(auth):
    manifest = {**PORTAL, "auth": auth}
    transport, recorder = transport_for(json_response([]))
    await run(
        generic_http.driver,
        "list_unpaid",
        {"older_than_days": 1},
        transport=transport,
        manifest=manifest,
        connector_id="invoice_portal",
    )
    sent = recorder.request.headers
    assert "Authorization" not in sent
    assert "X-Api-Key" not in sent
    assert SENTINEL not in str(dict(sent))


# ---------------------------------------------------------------------------
# generic_http — configuration
# ---------------------------------------------------------------------------


def test_a_manifest_without_a_base_url_is_not_configured():
    """Every manifest written before this driver existed keeps mocking."""
    legacy = {k: v for k, v in PORTAL.items() if k != "base_url"}
    assert generic_http.driver.configured(legacy, settings()) is False
    assert generic_http.driver.configured(PORTAL, settings()) is True


def test_an_action_without_a_path_is_not_supported():
    assert generic_http.driver.supports("mocked_only", PORTAL) is False
    assert generic_http.driver.supports("nope", PORTAL) is False
    assert generic_http.driver.supports("list_unpaid", PORTAL) is True


async def test_an_action_the_manifest_does_not_describe_is_reported_not_raised():
    outcome = await run(
        generic_http.driver,
        "mocked_only",
        {},
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        manifest=PORTAL,
        connector_id="invoice_portal",
    )
    assert outcome.ok is False
    assert "describes no call" in outcome.error


# ---------------------------------------------------------------------------
# crm / ticketing — placeholders, routed only when a manifest points somewhere
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("driver", [crm.driver, ticketing.driver])
def test_the_placeholder_connectors_mock_until_their_manifest_names_a_host(driver):
    assert driver.configured({}, settings()) is False
    assert driver.configured({"base_url": "https://desk.internal"}, settings()) is True


async def test_ticketing_with_a_base_url_calls_through_generic_http():
    manifest = {
        "auth": "api_key",
        "api_key_header": "X-Desk-Key",
        "base_url": "https://desk.internal",
        "actions": [{"name": "list_open", "method": "GET", "path": "/tickets/open"}],
    }
    transport, recorder = transport_for(json_response([{"id": "t-100"}]))
    outcome = await run(
        ticketing.driver, "list_open", {}, transport=transport, manifest=manifest,
        connector_id="ticketing",
    )
    assert recorder.request.url.path == "/tickets/open"
    assert recorder.request.headers["X-Desk-Key"] == SENTINEL
    assert outcome.result == [{"id": "t-100"}]


# ---------------------------------------------------------------------------
# Failure handling — envelopes, never exceptions
# ---------------------------------------------------------------------------


async def test_a_401_becomes_an_error_envelope():
    transport, _ = transport_for(json_response({"error": {"code": "InvalidAuthenticationToken"}}, 401))
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert outcome.ok is False
    assert outcome.status == 401
    assert "HTTP 401" in outcome.error
    assert "InvalidAuthenticationToken" in outcome.error


async def test_a_404_becomes_an_error_envelope_and_is_not_retried():
    transport, recorder = transport_for(json_response({"error": "nope"}, 404))
    outcome = await run(
        graph.driver, "draft_reply", {"message_id": "m1", "body": "b"}, transport=transport
    )
    assert (outcome.ok, outcome.status) == (False, 404)
    assert len(recorder.requests) == 1


async def test_a_500_is_retried_and_then_reported(instant_retries):
    transport, recorder = transport_for(json_response({"error": "boom"}, 500))
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert len(recorder.requests) == base.RETRY_ATTEMPTS
    assert outcome.ok is False
    assert outcome.status == 500


async def test_a_500_that_recovers_returns_the_recovered_result(instant_retries):
    replies = [httpx.Response(503, text="try later"), httpx.Response(200, json=GRAPH_MESSAGES)]
    transport, recorder = transport_for(lambda request: replies.pop(0))
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert len(recorder.requests) == 2
    assert outcome.ok is True
    assert outcome.result[0]["subject"] == "Invoice #4421"


async def test_a_connection_error_is_retried_and_then_reported(instant_retries):
    transport, recorder = transport_for(
        lambda request: httpx.ConnectError("connection refused", request=request)
    )
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert len(recorder.requests) == base.RETRY_ATTEMPTS
    assert outcome.ok is False
    assert outcome.status is None
    assert "could not reach the vendor" in outcome.error


async def test_a_read_timeout_is_reported_without_retrying(instant_retries):
    """Only connection-level failures are safe to repeat blindly."""
    transport, recorder = transport_for(
        lambda request: httpx.ReadTimeout("too slow", request=request)
    )
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert len(recorder.requests) == 1
    assert outcome.ok is False
    assert "could not reach the vendor" in outcome.error


async def test_generic_http_failures_use_the_same_envelope(instant_retries):
    transport, _ = transport_for(json_response({"detail": "denied"}, 403))
    outcome = await run(
        generic_http.driver,
        "list_unpaid",
        {"older_than_days": 1},
        transport=transport,
        manifest=PORTAL,
        connector_id="invoice_portal",
    )
    assert (outcome.ok, outcome.status) == (False, 403)
    assert "invoice_portal list_unpaid failed with HTTP 403" in outcome.error


def test_the_retry_policy_matches_the_model_router():
    from app.services import model_router

    assert base.RETRY_ATTEMPTS == model_router.RETRY_ATTEMPTS
    ours, theirs = base._retrying(), model_router._retrying()
    assert ours.stop.max_attempt_number == theirs.stop.max_attempt_number
    for field in ("multiplier", "min", "max", "exp_base"):
        assert getattr(ours.wait, field) == getattr(theirs.wait, field)
    assert isinstance(ours.wait, wait_exponential)
    assert ours.reraise is True


def test_only_connection_errors_and_5xx_are_retryable():
    request = httpx.Request("GET", "https://graph.test/v1.0/me/messages")
    assert base.is_retryable(httpx.ConnectError("x", request=request)) is True
    assert base.is_retryable(httpx.ConnectTimeout("x", request=request)) is True
    assert base.is_retryable(base._RetryableStatus(httpx.Response(503, request=request))) is True
    assert base.is_retryable(httpx.ReadTimeout("x", request=request)) is False
    assert base.is_retryable(ValueError("x")) is False


async def test_the_configured_request_timeout_is_applied():
    transport, recorder = transport_for(json_response({"value": []}))
    await run(
        graph.driver, "list_inbox", {}, transport=transport, settings=settings(request_timeout_seconds=3.25)
    )
    assert recorder.request.extensions["timeout"] == {
        "connect": 3.25,
        "read": 3.25,
        "write": 3.25,
        "pool": 3.25,
    }


# ---------------------------------------------------------------------------
# The leak guarantee at runtime
# ---------------------------------------------------------------------------


async def test_a_vendor_that_echoes_our_header_back_does_not_leak_it():
    """Chatty gateways quote the request they rejected. Redact before returning."""
    transport, _ = transport_for(
        lambda request: httpx.Response(
            401, text=f"rejected request with header Authorization: Bearer {SENTINEL}"
        )
    )
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert outcome.ok is False
    assert SENTINEL not in str(outcome)
    assert base.REDACTED in outcome.error


async def test_a_transport_error_carrying_the_request_does_not_leak_it(instant_retries):
    """`httpx` error reprs can carry the request — and therefore our header."""

    def explode(request: httpx.Request) -> httpx.Response:
        # Worst case: the message itself quotes the header we sent.
        raise httpx.ConnectError(
            f"failed sending headers {dict(request.headers)}", request=request
        )

    transport = httpx.MockTransport(explode)
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert outcome.ok is False
    assert SENTINEL not in str(outcome)
    assert SENTINEL not in repr(outcome)


async def test_nothing_in_a_successful_live_call_is_logged_or_returned(caplog):
    caplog.set_level(logging.DEBUG)
    transport, _ = transport_for(json_response(GRAPH_MESSAGES))
    outcome = await run(graph.driver, "list_inbox", {"top": 2}, transport=transport)

    assert SENTINEL not in repr(outcome)
    assert SENTINEL not in caplog.text


async def test_nothing_in_a_failed_live_call_is_logged(caplog, instant_retries):
    caplog.set_level(logging.DEBUG)
    transport, _ = transport_for(json_response({"error": SENTINEL}, 500))
    outcome = await run(graph.driver, "list_inbox", {}, transport=transport)

    assert outcome.ok is False
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Bearer {SENTINEL}", f"Bearer {base.REDACTED}"),
        (f"a {SENTINEL} b {SENTINEL}", f"a {base.REDACTED} b {base.REDACTED}"),
        ("nothing to do", "nothing to do"),
        ("", ""),
    ],
)
def test_redact_removes_every_occurrence(text, expected):
    assert base.redact(text, SENTINEL) == expected


def test_redact_will_not_blank_a_document_for_a_trivial_credential():
    """A 3-character 'secret' would turn redaction into censorship."""
    assert base.redact("the cat sat", "at") == "the cat sat"


# ---------------------------------------------------------------------------
# The audit: no secret-bearing name reaches a return, a log, a print, a raise
# ---------------------------------------------------------------------------

#: Identifiers that may hold credential material.
SECRETISH = re.compile(
    r"credential|secret|token|api_?key|password|bearer|authorization|request_headers",
    re.IGNORECASE,
)

#: Calls whose result cannot carry the credential through, so the audit stops
#: descending at them. `bool(credentials)` is how the service reports *that* it
#: authenticated without reporting *what with*; `redact(...)` is the scrubber.
SANITISERS = {"bool", "len", "isinstance", "redact", "_redact", "id", "type", "sorted"}

#: Functions that necessarily hold a secret in their hands. Their guarantee is
#: proven by runtime tests, not by this static rule:
#:
#: * `redact` handles the credential in order to remove it (proven above).
#: * `_sidecar_headers` builds the desktop control-plane auth header, so the
#:   token is the return value by construction.
#: * `_sidecar_secure_env` puts that same token into an ACI container group as a
#:   *secure* environment variable — write-only in ARM, absent from
#:   `az container show`. `test_desktop_service.py` asserts it lands only in
#:   `secure_value`, and never in a returned dict, a log line, or a `last_error`
#:   the API hands back.
#:
#: Everything else in those modules is audited normally, which is the point: it
#: is the *rest* of the file that must never grow a path from the token to a
#: response or a log.
#: `_aci_client` builds an `azure.identity` credential *object* and hands it to
#: the ARM SDK client it returns. The audit flags the local name because it
#: matches /credential/, but the thing being returned is a token **provider**,
#: not a token: it holds no secret of ours, and it exists precisely so that no
#: key is stored anywhere. Renaming the variable to dodge the regex would be
#: worse than exempting it — it would hide the pattern from the next reader
#: while changing nothing about the risk. The function is three lines of
#: construction with no logging of the credential and no other data flow.
EXEMPT_FUNCTIONS = {"redact", "_sidecar_headers", "_sidecar_secure_env", "_aci_client"}

LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


#: Beyond the vendor package: every module that handles a resolved credential
#: or renders something a human reads. `simulation.py` resolve-*checks* a
#: binding without fetching it and puts the answer in a plan; `undo.py` resolves
#: one to run a compensating call. Both produce records the UI shows, so both
#: are held to the same rule as the drivers.
#:
#: `desktop.py` joined them with the ACI driver. It holds one secret — the
#: sidecar shared token — and it now hands that token to Azure inside a
#: container group definition, reads container groups back, and writes operator
#: prose into `last_error` that the API returns verbatim. That is three new ways
#: for a secret to escape into something a human reads, which is exactly what
#: this audit is for.
AUDITED_SERVICES = ("connectors.py", "desktop.py", "simulation.py", "undo.py")


def audited_files() -> list[Path]:
    return [
        *sorted(VENDORS_DIR.rglob("*.py")),
        *(APP / "services" / name for name in AUDITED_SERVICES),
    ]


def _is_secretish(name: str) -> bool:
    return bool(SECRETISH.search(name))


def _escaping_names(node: ast.AST) -> set[str]:
    """Every name an expression exposes, descending into call arguments.

    A callee is not a value (`resolve_connector_secrets(...)` exposes nothing),
    and a sanitiser call exposes nothing at all. Everything else counts, so
    `raise ValueError(f"bad {credential}")` is caught.
    """
    found: set[str] = set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in SANITISERS:
            return set()
        if isinstance(func, ast.Attribute):
            found |= _escaping_names(func.value)
        for child in [*node.args, *(kw.value for kw in node.keywords)]:
            found |= _escaping_names(child)
        return found
    for child in ast.iter_child_nodes(node):
        found |= _escaping_names(child)
    return found


def _carried_names(node: ast.AST) -> set[str]:
    """Names whose value flows *into* the assigned object.

    Containers, f-strings, concatenations and aliases carry their operands; a
    call does not, because it builds something new — `await client.request(
    headers=request_headers)` yields a response, not a header. That boundary is
    where the runtime leak tests take over from the static ones.
    """
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Call):
        return set()
    found: set[str] = set()
    for child in ast.iter_child_nodes(node):
        found |= _carried_names(child)
    return found


def _scope_nodes(scope: ast.AST):
    """Every node in this scope, not descending into nested function bodies."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield child
        yield from _scope_nodes(child)


def _nested_scopes(scope: ast.AST):
    """Functions defined directly in this scope — class bodies included."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child
        else:
            yield from _nested_scopes(child)


def _assigned_targets(stmt: ast.AST) -> tuple[list[str], ast.AST | None]:
    names: list[str] = []
    value: ast.AST | None = getattr(stmt, "value", None)
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    for target in targets:
        node = target
        while isinstance(node, (ast.Subscript, ast.Attribute)):
            node = node.value
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            names += [e.id for e in node.elts if isinstance(e, ast.Name)]
    return names, value


def _is_log_sink(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        receiver = func.value
        return isinstance(receiver, ast.Name) and "log" in receiver.id.lower()
    return False


def audit_scope(scope: ast.AST, inherited: set[str], where: str) -> list[str]:
    """Report every place a secret-bearing name escapes this scope."""
    findings: list[str] = []
    if getattr(scope, "name", None) in EXEMPT_FUNCTIONS:
        return findings

    tainted = set(inherited)
    for _ in range(2):  # a second pass settles chained assignments
        for stmt in _scope_nodes(scope):
            names, value = _assigned_targets(stmt)
            if value is None or not names:
                continue
            carried = _carried_names(value)
            if any(_is_secretish(n) or n in tainted for n in carried):
                tainted |= set(names)

    def escaping(node: ast.AST) -> set[str]:
        return {n for n in _escaping_names(node) if _is_secretish(n) or n in tainted}

    for node in _scope_nodes(scope):
        line = getattr(node, "lineno", "?")
        leaked: set[str] = set()
        if isinstance(node, ast.Return) and node.value is not None:
            leaked = escaping(node.value)
            if leaked:
                findings.append(f"{where}:{line} returns {sorted(leaked)}")
        elif isinstance(node, ast.Call) and _is_log_sink(node):
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                leaked |= escaping(arg)
            if leaked:
                findings.append(f"{where}:{line} logs {sorted(leaked)}")
        elif isinstance(node, ast.Raise) and node.exc is not None:
            leaked = escaping(node.exc)
            if leaked:
                findings.append(f"{where}:{line} raises with {sorted(leaked)}")

    for nested in _nested_scopes(scope):
        findings += audit_scope(nested, tainted, f"{where}/{nested.name}")
    return findings


def audit_source(source: str, where: str = "<snippet>") -> list[str]:
    return audit_scope(ast.parse(source), set(), where)


def test_the_audit_covers_the_vendor_package_and_the_dispatcher():
    covered = {p.name for p in audited_files()}
    assert {
        "base.py",
        "graph.py",
        "generic_http.py",
        "crm.py",
        "ticketing.py",
        "connectors.py",
        "desktop.py",
        "simulation.py",
        "undo.py",
    } <= covered


def test_every_audited_file_exists():
    missing = [str(p) for p in audited_files() if not p.exists()]
    assert missing == [], "the audit names a file that is not there"


def test_the_audit_is_not_vacuous():
    """It must actually be looking at code that handles credentials."""
    sources = "\n".join(p.read_text(encoding="utf-8") for p in audited_files())
    assert SECRETISH.search(sources), "no credential-bearing identifiers found — wrong files?"


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(credential):\n    return credential\n",
        "def f(credential):\n    return {'token': credential}\n",
        "def f(credential):\n    logger.info('using %s', credential)\n",
        "def f(credential):\n    print(credential)\n",
        "def f(credential):\n    raise ValueError(f'bad {credential}')\n",
        "def f(credential):\n    headers = {'A': f'Bearer {credential}'}\n    return headers\n",
        "def f(credential):\n    h = {'A': credential}\n    g = h\n    logger.debug(g)\n",
        "def f(api_key):\n    def inner():\n        return api_key\n    return inner()\n",
        # Strict on purpose: hand a credential to a call inside a `return` and
        # you cannot tell from here whether it comes back out. Assign first.
        "def f(secret):\n    return other(secret)\n",
        "class C:\n    def m(self, credential):\n        return credential\n",
    ],
)
def test_the_audit_catches_a_leak(snippet):
    assert audit_source(snippet), f"the audit missed a leak in:\n{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        # `bool(...)` reports *that* we authenticated, not what with.
        "def f(credential):\n    return bool(credential)\n",
        "def f(text, credential):\n    return redact(text, credential)\n",
        # A call builds something new; the response is not the header.
        "def f(credential):\n    r = call(headers={'A': credential})\n    return r\n",
        "def f():\n    secrets = resolve_secrets()\n    return len(secrets)\n",
    ],
)
def test_the_audit_does_not_cry_wolf(snippet):
    assert audit_source(snippet) == []


def test_no_secret_bearing_name_escapes_the_vendor_package_or_the_dispatcher():
    findings: list[str] = []
    for path in audited_files():
        findings += audit_source(path.read_text(encoding="utf-8"), where=path.name)
    assert findings == [], "a credential can reach a return / log / print / raise:\n" + "\n".join(findings)
