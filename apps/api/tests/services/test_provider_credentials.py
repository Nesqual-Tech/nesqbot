"""`app.services.provider_credentials` — API keys typed into the app.

Three properties matter: a stored key round-trips through encryption, a
saved credential shows up as an in-process override immediately (the writing
replica does not wait for `RELOAD_INTERVAL_SECONDS`), and the override never
survives a delete.
"""

from __future__ import annotations

from app.models import ProviderCredential
from app.services import provider_credentials


def test_encrypt_decrypt_round_trips():
    token = provider_credentials.encrypt("sk-super-secret")
    assert token != "sk-super-secret"
    assert provider_credentials.decrypt(token) == "sk-super-secret"


def test_decrypt_of_garbage_is_none_not_a_raise():
    assert provider_credentials.decrypt("not-a-fernet-token") is None


async def test_set_credential_updates_the_in_memory_override_immediately(db):
    assert provider_credentials.get_override("openai") is None
    await provider_credentials.set_credential(
        db, provider="openai", api_key="sk-abc123", base_url=None, user_id=None
    )
    override = provider_credentials.get_override("openai")
    assert override is not None
    assert override["api_key"] == "sk-abc123"


async def test_set_credential_persists_the_row_encrypted(db):
    await provider_credentials.set_credential(
        db, provider="anthropic", api_key="sk-ant-xyz", base_url=None, user_id=None
    )
    row = await db.get(ProviderCredential, "anthropic")
    assert row is not None
    assert row.api_key_encrypted != "sk-ant-xyz"
    assert provider_credentials.decrypt(row.api_key_encrypted) == "sk-ant-xyz"


async def test_set_credential_twice_overwrites_not_duplicates(db):
    await provider_credentials.set_credential(
        db, provider="google", api_key="first-key", base_url=None, user_id=None
    )
    await provider_credentials.set_credential(
        db, provider="google", api_key="second-key", base_url=None, user_id=None
    )
    rows = await provider_credentials.list_credentials(db)
    assert [r.provider for r in rows].count("google") == 1
    assert provider_credentials.get_override("google")["api_key"] == "second-key"


async def test_delete_credential_removes_the_row_and_the_override(db):
    await provider_credentials.set_credential(
        db, provider="openai", api_key="sk-to-delete", base_url=None, user_id=None
    )
    await provider_credentials.delete_credential(db, provider="openai")
    assert provider_credentials.get_override("openai") is None
    assert await db.get(ProviderCredential, "openai") is None


async def test_load_overrides_from_db_populates_from_existing_rows(db):
    await provider_credentials.set_credential(
        db, provider="azure", api_key="az-key", base_url="https://example.openai.azure.com", user_id=None
    )
    provider_credentials.reset_cache()
    assert provider_credentials.get_override("azure") is None  # cache dropped, row still in the db

    await provider_credentials.load_overrides_from_db(db)
    override = provider_credentials.get_override("azure")
    assert override["api_key"] == "az-key"
    assert override["base_url"] == "https://example.openai.azure.com"


async def test_a_row_that_fails_to_decrypt_is_skipped_not_raised(db, monkeypatch):
    await provider_credentials.set_credential(
        db, provider="openai", api_key="sk-real", base_url=None, user_id=None
    )
    # Simulate a JWT_SECRET rotation: the stored token no longer decrypts
    # under the (now different) derived key.
    monkeypatch.setattr(provider_credentials, "decrypt", lambda token: None)
    provider_credentials.reset_cache()
    await provider_credentials.load_overrides_from_db(db)
    assert provider_credentials.get_override("openai") is None
