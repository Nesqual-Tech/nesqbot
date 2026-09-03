"""`app.services.secrets` — reference parsing, and the leak guarantee.

The leak tests are strict on purpose: a resolved secret must never reach an API
response, an audit event, or a log line. Only the *reference* travels.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditEvent
from app.services import secrets

SENTINEL = "s3cr3t-value-must-never-be-echoed-9f21ab"
ENV_VAR = "NESQ_TEST_CONNECTOR_SECRET"
REF = f"env://{ENV_VAR}"


@pytest.fixture(autouse=True)
def _clean_secret_cache(monkeypatch):
    secrets.reset_cache()
    monkeypatch.setenv(ENV_VAR, SENTINEL)
    yield
    secrets.reset_cache()


# ---------------------------------------------------------------------------
# parse_ref
# ---------------------------------------------------------------------------


def test_parse_an_env_ref():
    assert secrets.parse_ref("env://MY_VAR") == ("env", "", "MY_VAR")


def test_parse_a_kv_ref_with_a_bare_vault_name():
    kind, location, name = secrets.parse_ref("kv://my-vault/graph-secret")
    assert (kind, name) == ("kv", "graph-secret")
    assert location == "https://my-vault.vault.azure.net"


def test_parse_a_kv_ref_with_a_full_hostname():
    kind, location, name = secrets.parse_ref("kv://my-vault.vault.azure.net/graph-secret")
    assert (kind, name) == ("kv", "graph-secret")
    assert location == "https://my-vault.vault.azure.net"


@pytest.mark.parametrize("ref", ["", "   ", None, "env://", "kv://", "kv://vault", "kv:///name"])
def test_unparseable_refs_return_none(ref):
    assert secrets.parse_ref(ref) is None


def test_a_bare_name_needs_a_configured_default_vault():
    """AZURE_KEY_VAULT_URL is empty in this configuration."""
    assert secrets.parse_ref("just-a-name") is None


# ---------------------------------------------------------------------------
# resolve_secret
# ---------------------------------------------------------------------------


async def test_an_env_ref_resolves_locally():
    assert await secrets.resolve_secret(REF) == SENTINEL


async def test_a_missing_env_var_resolves_to_none():
    assert await secrets.resolve_secret("env://NESQ_DEFINITELY_NOT_SET_12345") is None


async def test_an_unresolvable_ref_returns_none_rather_than_raising():
    assert await secrets.resolve_secret("kv://no-such-vault/no-such-secret") is None
    assert await secrets.resolve_secret("nonsense") is None


async def test_resolution_is_cached():
    assert await secrets.resolve_secret(REF) == SENTINEL
    import os

    os.environ[ENV_VAR] = "changed-after-first-read"
    try:
        assert await secrets.resolve_secret(REF) == SENTINEL, "the cache should serve the first value"
    finally:
        os.environ[ENV_VAR] = SENTINEL


async def test_reset_cache_forces_a_re_read():
    await secrets.resolve_secret(REF)
    import os

    os.environ[ENV_VAR] = "rotated"
    try:
        secrets.reset_cache()
        assert await secrets.resolve_secret(REF) == "rotated"
    finally:
        os.environ[ENV_VAR] = SENTINEL


async def test_forget_re_reads_the_one_ref_it_was_given():
    """Replacing a credential in the app keeps the same reference — that is the
    design — so the cache, which is keyed on the reference and not the version,
    would serve the replaced value for the next `CACHE_TTL_SECONDS`. The first
    thing anyone does after a replace is run the connector."""
    import os

    await secrets.resolve_secret(REF)
    os.environ[ENV_VAR] = "replaced"
    try:
        secrets.forget(REF)
        assert await secrets.resolve_secret(REF) == "replaced"
    finally:
        os.environ[ENV_VAR] = SENTINEL


async def test_forget_leaves_every_other_ref_cached():
    """What distinguishes `forget` from `reset_cache`: one replace must not
    make every other bot's connector re-read its credential from Key Vault."""
    import os

    other_var = "NESQ_TEST_OTHER_CONNECTOR_SECRET"
    other_ref = f"env://{other_var}"
    os.environ[other_var] = "other-value"
    try:
        assert await secrets.resolve_secret(REF) == SENTINEL
        assert await secrets.resolve_secret(other_ref) == "other-value"

        secrets.forget(REF)
        os.environ[other_var] = "changed-after-caching"
        assert await secrets.resolve_secret(other_ref) == "other-value", (
            "forgetting one ref evicted another"
        )
    finally:
        os.environ.pop(other_var, None)


@pytest.mark.parametrize("ref", ["", "not a ref at all", None])
def test_forget_is_a_no_op_for_something_that_is_not_a_ref(ref):
    """It is called from the write path with whatever reference a binding
    happened to hold, including none — it must never raise there."""
    secrets.forget(ref)


# ---------------------------------------------------------------------------
# `app://` — a value the app stored itself
# ---------------------------------------------------------------------------


def test_parse_an_app_ref():
    assert secrets.parse_ref("app://connector/abc/crm") == ("app", "", "connector/abc/crm")


@pytest.mark.parametrize("ref", ["app://", "app:///"])
def test_an_empty_app_ref_is_not_a_ref(ref):
    assert secrets.parse_ref(ref) is None


async def test_an_app_ref_resolves_to_nothing_without_a_session():
    """`inbound.py` resolves refs a person typed and has no session to give.
    A person cannot type an `app://` ref — only `store_connector_secret`
    writes one — so returning None beats raising in that caller."""
    assert await secrets.resolve_secret("app://connector/abc/crm") is None


@pytest.mark.parametrize(
    ("ref", "backend"),
    [
        ("kv://a-vault/a-secret", "key_vault"),
        ("app://connector/abc/crm", "app_encrypted"),
        ("env://SOME_VAR", "env"),
        (None, "none"),
        ("", "none"),
        ("a bare name with no vault configured", "none"),
    ],
)
def test_describe_backend_reads_the_shape_of_the_reference(ref, backend):
    """The UI shows this so nobody believes a credential reached Key Vault when
    it did not. Derived from the ref rather than stored beside it, so the two
    cannot disagree."""
    assert secrets.describe_backend(ref) == backend


def test_two_connector_ids_that_differ_only_by_an_illegal_character_get_different_vault_names():
    """Key Vault names allow no underscores, so `a_b` and `a-b` both slugify to
    `a-b`. Without the hash, two bindings would share one vault secret and
    overwrite each other's credential."""
    import uuid

    bot = uuid.uuid4()
    assert secrets.connector_secret_name(bot, "a_b") != secrets.connector_secret_name(bot, "a-b")


def test_a_connector_vault_name_is_legal_and_stable():
    """Illegal characters are a 400 from Key Vault at write time, i.e. a
    feature that fails only in production; and a name that is not stable per
    pair could not be found again on the next read."""
    import re
    import uuid

    bot = uuid.uuid4()
    name = secrets.connector_secret_name(bot, "microsoft_graph")
    assert re.fullmatch(r"[0-9a-zA-Z-]{1,127}", name), name
    assert name == secrets.connector_secret_name(bot, "microsoft_graph")


async def test_resolve_connector_secrets_for_a_bound_connector(db, make_user, make_bot, make_connector_binding):
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(bot, "microsoft_graph", status="connected", secret_ref=REF)

    resolved = await secrets.resolve_connector_secrets(db, bot.id, "microsoft_graph")
    assert resolved == {"secret": SENTINEL}


async def test_resolve_connector_secrets_is_empty_without_a_binding(db, make_user, make_bot):
    user = await make_user()
    bot = await make_bot(user)
    assert await secrets.resolve_connector_secrets(db, bot.id, "microsoft_graph") == {}


async def test_resolve_connector_secrets_is_empty_when_the_ref_does_not_resolve(
    db, make_user, make_bot, make_connector_binding
):
    user = await make_user()
    bot = await make_bot(user)
    await make_connector_binding(
        bot, "crm", status="connected", secret_ref="env://NESQ_NOT_SET_98765"
    )
    assert await secrets.resolve_connector_secrets(db, bot.id, "crm") == {}


# ---------------------------------------------------------------------------
# The leak guarantee — strict
# ---------------------------------------------------------------------------


async def test_a_resolved_secret_never_appears_in_an_api_response(
    authed, db, bot_a, make_connector_binding
):
    await make_connector_binding(bot_a, "microsoft_graph", status="connected", secret_ref=REF)
    # Prove the sentinel really is resolvable, so a leak would be detectable.
    assert await secrets.resolve_secret(REF) == SENTINEL

    responses = [
        await authed.get(f"/api/bots/{bot_a.id}/connectors"),
        await authed.post(
            f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/list_inbox",
            json={"input": {"top": 2}},
        ),
        await authed.post(
            f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/draft_reply",
            json={"input": {"message_id": "m1", "body": "hi"}},
        ),
        await authed.get("/api/audit"),
        await authed.get("/api/bots"),
    ]
    for response in responses:
        assert response.status_code in (200, 201)
        assert SENTINEL not in response.text, (
            f"{response.request.method} {response.request.url.path} leaked the resolved secret"
        )


async def test_the_binding_response_carries_the_reference_not_the_value(
    authed, bot_a, make_connector_binding
):
    await make_connector_binding(bot_a, "crm", status="connected", secret_ref=REF)
    body = (await authed.get(f"/api/bots/{bot_a.id}/connectors")).json()
    assert body[0]["secret_ref"] == REF
    assert SENTINEL not in str(body)


async def test_a_resolved_secret_never_reaches_an_audit_event(
    authed, db, bot_a, make_connector_binding
):
    await make_connector_binding(bot_a, "microsoft_graph", status="connected", secret_ref=REF)
    await authed.post(
        f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/list_inbox",
        json={"input": {"top": 1}},
    )
    rows = await db.execute(select(AuditEvent))
    for event in rows.scalars().all():
        assert SENTINEL not in str(event.detail), "an audit event carries the resolved secret"


async def test_an_authenticated_action_reports_only_that_it_was_authenticated(
    authed, bot_a, make_connector_binding
):
    await make_connector_binding(bot_a, "microsoft_graph", status="connected", secret_ref=REF)
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/list_inbox",
        json={"input": {"top": 1}},
    )
    body = response.json()
    assert body["ok"] is True
    assert body.get("authenticated") is True
    assert SENTINEL not in response.text


async def test_a_resolved_secret_never_reaches_a_log_line(
    authed, bot_a, make_connector_binding, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    await make_connector_binding(bot_a, "microsoft_graph", status="connected", secret_ref=REF)
    await authed.post(
        f"/api/bots/{bot_a.id}/connectors/microsoft_graph/actions/list_inbox",
        json={"input": {"top": 1}},
    )
    assert SENTINEL not in caplog.text, "the resolved secret was logged"


async def test_an_approved_action_does_not_leak_the_secret_either(
    authed, bot_a, make_approval, make_connector_binding
):
    await make_connector_binding(bot_a, "microsoft_graph", status="connected", secret_ref=REF)
    approval = await make_approval(
        bot_a,
        risk="send",
        payload={
            "kind": "connector_action",
            "connector_id": "microsoft_graph",
            "action": "send_mail",
            "input": {"to": "a@b.c", "subject": "s", "body": "b"},
        },
    )
    response = await authed.post(
        f"/api/approvals/{approval.id}/decide", json={"decision": "approved"}
    )
    assert response.status_code == 200
    assert response.json()["execution"]["ok"] is True
    assert SENTINEL not in response.text
