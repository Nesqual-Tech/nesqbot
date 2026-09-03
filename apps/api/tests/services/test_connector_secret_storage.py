"""Storing a connector credential the app was *given*, not one it was told about.

Until this landed, a credential had to exist in Key Vault before anyone could
use it here: a human created the secret by hand and pasted a `kv://` reference
into the app. `POST /bots/{bot_id}/connectors/{connector_id}/secret` takes the
value itself.

Two backends, and which one took the value is the thing under test as much as
the storage is. Key Vault is preferred, but writing to it needs the "Key Vault
Secrets Officer" role and the deployed managed identity holds the read-only
"Key Vault Secrets User" (`infra/azure/main.bicep`), so the honest outcome on
today's deployment is the encrypted-in-Postgres fallback that
`provider_credentials.py` already established for provider API keys. A caller
that is told "saved" without being told *where* is a caller who will believe
their key reached a vault it never reached.

The leak register here is `test_secrets.py`'s, deliberately: a value handed to
the app must never come back out — not in a response, not in an audit row, not
in a log line.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import AuditEvent, BotConnector, ProviderCredential
from app.services import provider_credentials, secrets

#: Unmistakable in a haystack: every leak assertion below is a substring search.
SENTINEL = "typed-into-the-app-3f9c2a-never-echo-me"
REPLACEMENT = "the-second-credential-8b71de-never-echo-me"

VAULT_URL = "https://nesq-test-vault.vault.azure.net"


class FakeVault:
    """The two `SecretClient` methods this feature uses, and nothing else.

    Injected by monkeypatching `secrets._get_client`, which is the seam
    `test_secrets.py` already relies on by *not* patching it — every Key Vault
    test in that file passes because the real `_get_client` returns None
    without an Azure credential.
    """

    def __init__(self, *, refuse: Exception | None = None) -> None:
        self.stored: dict[str, str] = {}
        self.refuse = refuse
        self.writes = 0

    async def set_secret(self, name: str, value: str):
        self.writes += 1
        if self.refuse is not None:
            raise self.refuse
        self.stored[name] = value
        return SimpleNamespace(name=name)

    async def get_secret(self, name: str):
        if name not in self.stored:
            raise LookupError(name)
        return SimpleNamespace(value=self.stored[name])


def _forbidden() -> Exception:
    """What Key Vault raises for an identity with read-only access.

    Built through the real `HttpResponseError` rather than a stand-in, because
    `secrets._write_failure_reason` reads `status_code` off it and a stand-in
    would prove only that the stand-in works.
    """
    from azure.core.exceptions import HttpResponseError

    response = SimpleNamespace(
        status_code=403,
        headers={},
        reason="Forbidden",
        content_type="application/json",
        text=lambda *_args: "{}",
    )
    return HttpResponseError(message="Caller is not authorized to perform action", response=response)


@pytest.fixture
def vault(monkeypatch):
    """A configured vault that accepts writes, holding the refs these tests cite.

    `stored` is pre-seeded because binding a ref now *checks that the name
    exists* (`secrets.check_ref`) instead of judging the string's shape — a
    pasted credential and a real secret name are both
    `^[0-9a-zA-Z-]{1,127}$`, so shape could never separate them. A vault that
    answers "yes" to every name is therefore no longer a realistic double: it
    would make the guard untestable in exactly the direction that leaks.
    """
    client = FakeVault()
    client.stored.update(
        {
            "crm-key": "the-real-crm-key",
            "crm-key-in-the-default-vault": "the-real-crm-key",
            "hand-made-secret": "made-by-hand-in-the-portal",
        }
    )
    monkeypatch.setattr(secrets, "get_settings", lambda: SimpleNamespace(azure_key_vault_url=VAULT_URL))
    monkeypatch.setattr(secrets, "_get_client", lambda _url: client)
    secrets.reset_cache()
    yield client
    secrets.reset_cache()


@pytest.fixture
def read_only_vault(monkeypatch):
    """A configured vault whose identity may read but not write.

    Same pre-seeding as `vault`: reads are what the ref check uses, and this
    identity can read.
    """
    client = FakeVault(refuse=_forbidden())
    client.stored.update({"crm-key": "the-real-crm-key"})
    monkeypatch.setattr(secrets, "get_settings", lambda: SimpleNamespace(azure_key_vault_url=VAULT_URL))
    monkeypatch.setattr(secrets, "_get_client", lambda _url: client)
    secrets.reset_cache()
    yield client
    secrets.reset_cache()


@pytest.fixture
def no_vault(monkeypatch):
    """A deployment with no Key Vault at all — every laptop, and self-hosters."""
    monkeypatch.setattr(secrets, "get_settings", lambda: SimpleNamespace(azure_key_vault_url=""))
    secrets.reset_cache()
    yield
    secrets.reset_cache()


async def _binding(db, bot_id, connector_id="crm") -> BotConnector | None:
    rows = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id, BotConnector.connector_id == connector_id
        )
    )
    return rows.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Key Vault took it
# ---------------------------------------------------------------------------


async def test_a_vault_that_accepts_the_write_leaves_only_a_reference_behind(authed, db, bot_a, vault):
    """The whole point of preferring Key Vault: the app holds a `kv://` ref and
    the value lives in the vault. If the binding row ever held the value
    instead, `GET /bots/{id}/connectors` would hand it to every user who can
    see the bot."""
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["backend"] == "key_vault"
    assert body["secret_ref"].startswith("kv://nesq-test-vault.vault.azure.net/")

    # One write, and it is the value that was handed over. Asserted this way
    # rather than as the vault's whole contents because the fixture now
    # pre-seeds the names other tests reference — a vault that answers "yes" to
    # every name cannot exercise `secrets.check_ref`.
    assert vault.writes == 1
    assert SENTINEL in vault.stored.values()

    link = await _binding(db, bot_a.id)
    assert link is not None
    assert link.secret_ref == body["secret_ref"]
    assert link.status == "connected"


async def test_a_key_vault_backed_credential_resolves_for_the_connector(authed, db, bot_a, vault):
    """Storing it is only half the feature — the bot has to be able to use it."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    assert await secrets.resolve_connector_secrets(db, bot_a.id, "crm") == {"secret": SENTINEL}


async def test_nothing_is_written_to_postgres_when_the_vault_took_it(authed, db, bot_a, vault):
    """The fallback table must stay empty on the happy path, or the value would
    be at rest in two places and only one of them would ever be revoked."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    rows = await db.execute(select(ProviderCredential))
    assert rows.scalars().all() == []


# ---------------------------------------------------------------------------
# The vault refused — degrade honestly
# ---------------------------------------------------------------------------


async def test_a_read_only_identity_falls_back_to_encryption_and_names_the_backend(
    authed, db, bot_a, read_only_vault
):
    """This is production today. The write is attempted, Key Vault answers 403
    because the identity has "Key Vault Secrets User" and not "Secrets
    Officer", and the request must still succeed while saying plainly that the
    value did not reach the vault."""
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert read_only_vault.writes == 1, "Key Vault should be tried first, not skipped"
    assert body["backend"] == "app_encrypted"
    assert body["secret_ref"] == f"app://connector/{bot_a.id}/crm"
    assert "Key Vault Secrets Officer" in body["detail"], (
        "the failure has to name the role that would fix it"
    )


async def test_the_fallback_stores_a_fernet_token_not_the_value(authed, db, bot_a, read_only_vault):
    """`bot_connectors.secret_ref` was designed to hold a reference and is
    echoed back by the listing endpoint, so the ciphertext must not land there
    either — it goes to the column `provider_credentials.py` already encrypts
    into."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})

    link = await _binding(db, bot_a.id)
    assert link is not None
    assert link.secret_ref == f"app://connector/{bot_a.id}/crm"
    assert SENTINEL not in (link.secret_ref or "")

    rows = await db.execute(select(ProviderCredential))
    stored = rows.scalars().all()
    assert len(stored) == 1
    assert stored[0].provider == f"app-secret:connector/{bot_a.id}/crm"
    assert stored[0].api_key_encrypted != SENTINEL
    assert SENTINEL not in stored[0].api_key_encrypted
    assert provider_credentials.decrypt(stored[0].api_key_encrypted) == SENTINEL


async def test_an_app_encrypted_credential_resolves_for_the_connector(
    authed, db, bot_a, read_only_vault
):
    """The fallback is only honest if it actually works — an `app://` ref has
    to resolve through the same call the connector runtime makes."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    assert await secrets.resolve_connector_secrets(db, bot_a.id, "crm") == {"secret": SENTINEL}


async def test_with_no_vault_configured_the_write_is_never_attempted(authed, db, bot_a, no_vault):
    """A laptop running `docker compose up` has no vault. It should not build a
    client, wait on IMDS, or report a Key Vault failure — it should say there
    is no vault."""
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL}
    )
    body = response.json()
    assert body["backend"] == "app_encrypted"
    assert "AZURE_KEY_VAULT_URL" in body["detail"]
    assert await secrets.resolve_connector_secrets(db, bot_a.id, "crm") == {"secret": SENTINEL}


async def test_the_listing_keeps_saying_which_backend_holds_it(authed, bot_a, read_only_vault):
    """The backend has to survive a reload. If it only existed in the write's
    response, the UI could say "encrypted here" for ninety seconds and then go
    quiet — and quiet reads as "it went to the vault"."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    listed = (await authed.get(f"/api/bots/{bot_a.id}/connectors")).json()
    assert [(row["connector_id"], row["secret_backend"]) for row in listed] == [("crm", "app_encrypted")]


async def test_a_pasted_kv_reference_still_reports_the_key_vault_backend(authed, bot_a, vault):
    """The reference path people already use must keep working and must be
    described the same way as one the app wrote itself."""
    await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm",
        json={"secret_ref": "kv://nesq-test-vault/hand-made-secret", "status": "connected"},
    )
    listed = (await authed.get(f"/api/bots/{bot_a.id}/connectors")).json()
    assert listed[0]["secret_backend"] == "key_vault"
    assert listed[0]["secret_ref"] == "kv://nesq-test-vault/hand-made-secret"


# ---------------------------------------------------------------------------
# Replacing one
# ---------------------------------------------------------------------------


async def test_replacing_a_credential_takes_effect_immediately(authed, db, bot_a, read_only_vault):
    """A replace keeps the same reference by design, and the resolver caches on
    the reference for `CACHE_TTL_SECONDS`. Without `secrets.forget`, the first
    thing anyone does after replacing a credential — run the connector to see
    whether it worked — is answered with the credential they just replaced, for
    five minutes."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    assert await secrets.resolve_connector_secrets(db, bot_a.id, "crm") == {"secret": SENTINEL}

    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": REPLACEMENT})
    assert await secrets.resolve_connector_secrets(db, bot_a.id, "crm") == {"secret": REPLACEMENT}


async def test_unbinding_throws_the_encrypted_value_away(authed, db, bot_a, read_only_vault):
    """An app-encrypted value with no binding pointing at it is invisible and
    still decryptable by anyone holding `JWT_SECRET`. Unbind means gone."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    assert (await authed.delete(f"/api/bots/{bot_a.id}/connectors/crm")).status_code == 200

    rows = await db.execute(select(ProviderCredential))
    assert rows.scalars().all() == [], "the encrypted credential outlived its binding"


# ---------------------------------------------------------------------------
# `secret_ref` is for references. The guard.
# ---------------------------------------------------------------------------


async def test_pasting_the_credential_into_the_reference_field_is_refused(authed, db, bot_a, vault):
    """`inbound.py` learned this for the identically-shaped column on inbound
    sources: a value stored here is echoed back by `GET /bots/{id}/connectors`
    to every user who can see the bot, forever. This route is the more likely
    trap, because the only input the app used to offer was labelled "Secret
    ref"."""
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm",
        json={"secret_ref": "sk-live-51H8xQ2eZvKYlo0hunter2", "status": "connected"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_secret_ref"
    assert await _binding(db, bot_a.id) is None, "a refused bind must not write a row"


async def test_the_refusal_names_the_endpoint_that_does_take_a_value(authed, bot_a, vault):
    """A guard that only says no sends the person back to the same field with
    the same key. It has to say where the value goes instead."""
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm",
        json={"secret_ref": "AKIAIOSFODNN7EXAMPLE+wJalrXUtnFEMI", "status": "connected"},
    )
    assert response.status_code == 422
    assert "/secret" in response.json()["detail"]


@pytest.mark.parametrize(
    "ref",
    [
        "kv://nesq-test-vault/crm-key",
        "kv://nesq-test-vault.vault.azure.net/crm-key",
        "env://NESQ_CRM_KEY",
        "crm-key-in-the-default-vault",
    ],
)
async def test_every_real_reference_shape_still_binds(
    authed, db, bot_a, vault, monkeypatch, ref
):
    """The guard must not cost anyone their existing workflow. The bare name is
    in this list only because a vault is configured in this fixture — with none
    configured it cannot resolve, and is refused.

    Each shape names something that really exists: the vault fixture holds the
    two `kv://` names and the bare one, and the environment variable is set
    here. That is the point of the guard rather than an inconvenience of it — a
    reference to a secret nobody has is a typo at best and a pasted credential
    at worst, and `secrets.check_ref` refuses both."""
    monkeypatch.setenv("NESQ_CRM_KEY", "the-real-crm-key")
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm", json={"secret_ref": ref, "status": "connected"}
    )
    assert response.status_code == 200, response.text
    link = await _binding(db, bot_a.id)
    assert link is not None and link.secret_ref == ref


async def test_rebinding_a_connector_whose_value_the_app_stored_is_not_refused(
    authed, db, bot_a, read_only_vault
):
    """The server writes `app://…` into `secret_ref` itself. If the guard did
    not recognise the scheme, changing a binding's *status* after storing a
    value — which sends the ref back unchanged — would 422 on the app's own
    marker."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    ref = f"app://connector/{bot_a.id}/crm"

    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm", json={"secret_ref": ref, "status": "disconnected"}
    )
    assert response.status_code == 200, response.text
    link = await _binding(db, bot_a.id)
    assert link is not None and link.secret_ref == ref
    assert await secrets.resolve_connector_secrets(db, bot_a.id, "crm") == {"secret": SENTINEL}


async def test_clearing_the_reference_is_still_allowed(authed, db, bot_a, vault):
    """`secret_ref: null` means "this connector needs no credential" and is how
    a binding is made without one. The guard only refuses non-references."""
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm", json={"status": "connected"}
    )
    assert response.status_code == 200
    link = await _binding(db, bot_a.id)
    assert link is not None and link.secret_ref is None


# ---------------------------------------------------------------------------
# The leak guarantee — the value the app was handed never comes back
# ---------------------------------------------------------------------------


async def test_a_submitted_credential_never_comes_back_out(authed, db, bot_a, read_only_vault, caplog):
    """The strict sweep. The fallback path is the one under test because it is
    the one that puts the value in this deployment's own database: the response
    that stored it, every listing afterwards, the audit trail in both its API
    and its row form, and the log at DEBUG."""
    import logging

    caplog.set_level(logging.DEBUG)

    stored = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL}
    )
    assert stored.status_code == 200
    # Prove the value really was stored, so a leak would have something to leak.
    assert await secrets.resolve_connector_secrets(db, bot_a.id, "crm") == {"secret": SENTINEL}

    responses = [
        stored,
        await authed.get(f"/api/bots/{bot_a.id}/connectors"),
        await authed.get("/api/audit"),
        await authed.post(
            f"/api/bots/{bot_a.id}/connectors/crm/actions/search_accounts",
            json={"input": {"query": "acme"}},
        ),
    ]
    for response in responses:
        assert response.status_code in (200, 201), response.text
        assert SENTINEL not in response.text, (
            f"{response.request.method} {response.request.url.path} echoed the submitted credential"
        )

    rows = await db.execute(select(AuditEvent))
    for event in rows.scalars().all():
        assert SENTINEL not in str(event.detail), "an audit row carries the submitted credential"

    assert SENTINEL not in caplog.text, "the submitted credential was logged"


async def test_the_audit_trail_records_where_it_went_without_recording_what_it_was(
    authed, db, bot_a, read_only_vault
):
    """The fact worth keeping is that somebody set a credential and which store
    took it — that is the record you want when asking why a key is in Postgres
    rather than the vault."""
    await authed.post(f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL})
    rows = await db.execute(select(AuditEvent).where(AuditEvent.event_type == "connector_secret_set"))
    events = rows.scalars().all()
    assert len(events) == 1
    assert events[0].detail["backend"] == "app_encrypted"
    assert events[0].detail["connector_id"] == "crm"
    assert SENTINEL not in str(events[0].detail)


async def test_a_key_vault_write_failure_does_not_quote_the_request_back(
    authed, bot_a, monkeypatch, caplog
):
    """`_write_failure_reason` reports the exception's class and HTTP status,
    never its message. An Azure error renders the service's response body, and
    this module's rule about secret values is absolute — a `detail` built from
    `str(exc)` is one badly-behaved 400 away from publishing the credential."""
    import logging

    from azure.core.exceptions import HttpResponseError

    caplog.set_level(logging.DEBUG)
    client = FakeVault(refuse=HttpResponseError(message=f"rejected value {SENTINEL}"))
    monkeypatch.setattr(secrets, "get_settings", lambda: SimpleNamespace(azure_key_vault_url=VAULT_URL))
    monkeypatch.setattr(secrets, "_get_client", lambda _url: client)
    secrets.reset_cache()

    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/crm/secret", json={"value": SENTINEL}
    )
    assert response.status_code == 200
    assert response.json()["backend"] == "app_encrypted"
    assert SENTINEL not in response.text
    assert SENTINEL not in caplog.text


async def test_an_unknown_connector_is_a_404_before_anything_is_stored(authed, db, bot_a, vault):
    """Storing a credential for a connector that does not exist would leave a
    vault secret and a binding row nobody can ever reach or revoke."""
    response = await authed.post(
        f"/api/bots/{bot_a.id}/connectors/not_a_connector/secret", json={"value": SENTINEL}
    )
    assert response.status_code == 404
    assert vault.writes == 0
    rows = await db.execute(select(ProviderCredential))
    assert rows.scalars().all() == []
