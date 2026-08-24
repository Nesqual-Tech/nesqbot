"""Entra v2 access-token validation.

Everything here runs against **locally minted RS256 tokens and a fake JWKS** —
no network, no tenant, no credentials. `app.auth._fetch_jwks` is replaced with a
coroutine returning a key set we control, so the code under test does its real
signature verification against a key we hold the private half of.

Each rejection has its own test. A single "a bad token is refused" test would
pass even if five of the six checks silently disappeared: one surviving check
would still reject a token that violates all of them. So every token below is
valid in every respect *except* the one property the test is about.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from jose import jwt

from app.config import get_settings

TENANT_ID = "10000000-0000-0000-0000-000000000001"
API_APP_ID = "20000000-0000-0000-0000-000000000002"
CLIENT_APP_ID = "30000000-0000-0000-0000-000000000003"
SCOPE = "access_as_user"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"

SIGNING_KID = "test-signing-key"


def _rsa_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key: Any) -> str:
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()


def _b64bytes(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u(value: int) -> str:
    return _b64bytes(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwk(key: Any, kid: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u(numbers.n),
        "e": _b64u(numbers.e),
    }


#: Generated once — 2048-bit keygen is not free and none of these tests need a
#: fresh key. `_IMPOSTOR` is a second, unpublished key used for forgeries.
_SIGNING_KEY = _rsa_key()
_IMPOSTOR_KEY = _rsa_key()
_JWKS = {"keys": [_jwk(_SIGNING_KEY, SIGNING_KID)]}


def mint(
    *,
    key: Any = None,
    kid: str = SIGNING_KID,
    aud: Any = API_APP_ID,
    iss: str = ISSUER,
    scp: Any = SCOPE,
    exp_offset: int = 3600,
    oid: str | None = None,
    extra: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    """A v2-shaped Entra access token, valid unless a keyword says otherwise."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "aud": aud,
        "iss": iss,
        "iat": now - 30,
        "nbf": now - 30,
        "exp": now + exp_offset,
        "tid": TENANT_ID,
        "oid": oid or str(uuid.uuid4()),
        "sub": "subject-pairwise-id",
        "azp": CLIENT_APP_ID,
        "name": "Ada Lovelace",
        "preferred_username": "ada@example.com",
        "ver": "2.0",
    }
    if scp is not None:
        claims["scp"] = scp
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(
        claims,
        _pem(key or _SIGNING_KEY),
        algorithm="RS256",
        headers={"kid": kid},
    )


@pytest.fixture
def entra(monkeypatch):
    """A configured tenant plus a JWKS the suite owns the private key for."""
    import app.auth as auth_module

    configured = get_settings().model_copy(
        update={
            "azure_tenant_id": TENANT_ID,
            "azure_client_id": API_APP_ID,
            "azure_api_scope": SCOPE,
        }
    )
    monkeypatch.setattr(auth_module, "get_settings", lambda: configured)

    async def _fake_jwks(tenant_id: str, *, force: bool = False) -> dict[str, Any]:
        assert tenant_id == TENANT_ID, "the API must fetch keys for its own tenant only"
        return _JWKS

    monkeypatch.setattr(auth_module, "_fetch_jwks", _fake_jwks)
    auth_module.reset_jwks_cache()
    return configured


async def verify(token: str) -> dict[str, Any]:
    from app.auth import verify_entra_access_token

    return await verify_entra_access_token(token)


async def assert_rejected(token: str) -> None:
    """401, and a body that says nothing about which check failed."""
    from fastapi import HTTPException

    from app.auth import UNAUTHORIZED_DETAIL

    with pytest.raises(HTTPException) as caught:
        await verify(token)
    assert caught.value.status_code == 401
    assert caught.value.detail == UNAUTHORIZED_DETAIL


# ---------------------------------------------------------------------------
# The fixture itself — a mis-built token would make every rejection vacuous
# ---------------------------------------------------------------------------


def test_the_minted_token_is_shaped_like_a_real_v2_access_token():
    claims = jwt.get_unverified_claims(mint())
    assert claims["aud"] == API_APP_ID
    assert claims["iss"] == ISSUER
    assert claims["scp"] == SCOPE
    assert claims["ver"] == "2.0"
    assert jwt.get_unverified_header(mint())["kid"] == SIGNING_KID


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_a_valid_access_token_is_accepted(entra):
    claims = await verify(mint(oid="11111111-1111-1111-1111-111111111111"))
    assert claims["oid"] == "11111111-1111-1111-1111-111111111111"
    assert claims["preferred_username"] == "ada@example.com"


async def test_the_app_id_uri_spelling_of_the_audience_is_accepted(entra):
    """`aud` may be the GUID or `api://<guid>` — the same one resource."""
    assert await verify(mint(aud=f"api://{API_APP_ID}"))


async def test_scp_may_carry_several_scopes(entra):
    assert await verify(mint(scp=f"User.Read {SCOPE} Files.Read"))


async def test_a_token_inside_the_clock_skew_allowance_is_accepted(entra):
    """Expired by less than the configured skew: accepted, deliberately."""
    assert entra.entra_clock_skew_seconds >= 30
    assert await verify(mint(exp_offset=-10))


# ---------------------------------------------------------------------------
# One test per rejection
# ---------------------------------------------------------------------------


async def test_a_token_for_the_wrong_audience_is_rejected(entra):
    """The client's own id is the audience-confusion case this split prevents."""
    await assert_rejected(mint(aud=CLIENT_APP_ID))


async def test_a_token_from_another_issuer_is_rejected(entra):
    other = "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0"
    await assert_rejected(mint(iss=other))


async def test_the_legacy_v1_issuer_is_rejected(entra):
    """The registration issues v2 only; accepting v1 would widen for nothing."""
    await assert_rejected(mint(iss=f"https://sts.windows.net/{TENANT_ID}/"))


async def test_an_expired_token_is_rejected(entra):
    await assert_rejected(mint(exp_offset=-3600))


async def test_a_token_signed_by_an_unknown_key_is_rejected(entra):
    """Same `kid`, different private key: key selection succeeds, RSA does not."""
    await assert_rejected(mint(key=_IMPOSTOR_KEY))


async def test_a_token_naming_a_kid_that_is_not_in_the_jwks_is_rejected(entra):
    await assert_rejected(mint(kid="not-a-published-key"))


async def test_a_token_with_no_scp_claim_is_rejected(entra):
    """An ID token has no `scp` — it is not authorization to call anything."""
    await assert_rejected(mint(scp=None))


async def test_a_token_whose_scp_lacks_the_api_scope_is_rejected(entra):
    await assert_rejected(mint(scp="User.Read openid profile"))


async def test_an_app_only_token_with_roles_instead_of_scp_is_rejected(entra):
    """`roles` is application permission; this API is only called for a user."""
    await assert_rejected(mint(scp=None, extra={"roles": [SCOPE]}))


async def test_a_token_from_another_tenant_is_rejected(entra):
    foreign = "00000000-0000-0000-0000-000000000000"
    await assert_rejected(mint(extra={"tid": foreign}))


async def test_a_token_with_no_subject_is_rejected(entra):
    await assert_rejected(mint(drop=("oid", "sub")))


async def test_an_unsigned_token_is_rejected(entra):
    """`alg: none` is refused before a key is even selected."""
    now = int(time.time())
    header = _b64bytes(json.dumps({"alg": "none", "typ": "JWT", "kid": SIGNING_KID}).encode())
    payload = _b64bytes(
        json.dumps(
            {"aud": API_APP_ID, "iss": ISSUER, "scp": SCOPE, "exp": now + 600, "oid": "x"}
        ).encode()
    )
    await assert_rejected(f"{header}.{payload}.")


async def test_a_symmetrically_signed_token_is_rejected(entra):
    """HS256 signed with anything at all must not be accepted for RS256 keys."""
    now = int(time.time())
    forged = jwt.encode(
        {"aud": API_APP_ID, "iss": ISSUER, "scp": SCOPE, "exp": now + 600, "oid": "x"},
        "the-attacker-picks-this",
        algorithm="HS256",
        headers={"kid": SIGNING_KID},
    )
    await assert_rejected(forged)


async def test_garbage_is_rejected(entra):
    await assert_rejected("not-a-jwt")


async def test_an_empty_token_is_rejected(entra):
    await assert_rejected("   ")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


async def test_an_unconfigured_tenant_answers_503_not_401(monkeypatch):
    """Our own misconfiguration is not the caller's fault — and never a pass."""
    from fastapi import HTTPException

    import app.auth as auth_module

    blank = get_settings().model_copy(update={"azure_tenant_id": "", "azure_client_id": ""})
    monkeypatch.setattr(auth_module, "get_settings", lambda: blank)

    with pytest.raises(HTTPException) as caught:
        await verify(mint())
    assert caught.value.status_code == 503


async def test_a_blank_required_scope_does_not_degrade_into_accepting_anything(monkeypatch):
    from fastapi import HTTPException

    import app.auth as auth_module

    unscoped = get_settings().model_copy(
        update={"azure_tenant_id": TENANT_ID, "azure_client_id": API_APP_ID, "azure_api_scope": ""}
    )
    monkeypatch.setattr(auth_module, "get_settings", lambda: unscoped)

    with pytest.raises(HTTPException) as caught:
        await verify(mint(scp=None))
    assert caught.value.status_code == 503


def test_the_api_app_id_is_the_audience_not_the_client_id():
    """The setting is named `azure_client_id` but means the resource server.

    Getting these backwards is silent — a client-audienced token verifies against
    the same JWKS — so the mapping is pinned here as well as in the comment.
    """
    from app.auth import accepted_audiences

    assert accepted_audiences(API_APP_ID) == {API_APP_ID, f"api://{API_APP_ID}"}
    assert CLIENT_APP_ID not in accepted_audiences(API_APP_ID)
    assert accepted_audiences(f"api://{API_APP_ID}") == accepted_audiences(API_APP_ID)


def test_scopes_parse_out_of_either_shape():
    from app.auth import token_scopes

    assert token_scopes({"scp": "a b  c"}) == {"a", "b", "c"}
    assert token_scopes({"scp": ["a", "b"]}) == {"a", "b"}
    assert token_scopes({}) == set()
    assert token_scopes({"roles": ["a"]}) == set()


def test_only_the_v2_issuer_is_expected():
    from app.auth import expected_issuer

    assert expected_issuer(TENANT_ID) == ISSUER


# ---------------------------------------------------------------------------
# Through the endpoint
# ---------------------------------------------------------------------------


async def test_the_endpoint_exchanges_a_bearer_access_token_for_a_session(client, entra, db):
    from sqlalchemy import select

    from app.models import User

    oid = "22222222-2222-2222-2222-222222222222"
    response = await client.post(
        "/api/auth/entra",
        headers={"Authorization": f"Bearer {mint(oid=oid)}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["user"]["email"] == "ada@example.com"

    stored = (await db.execute(select(User).where(User.entra_oid == oid))).scalar_one()
    assert stored.email == "ada@example.com"


async def test_the_session_token_the_endpoint_returns_authenticates_requests(app, client, entra):
    from tests.conftest import _client_for

    token = (
        await client.post("/api/auth/entra", headers={"Authorization": f"Bearer {mint()}"})
    ).json()["access_token"]

    async with _client_for(app, {"Authorization": f"Bearer {token}"}) as session:
        me = await session.get("/api/me")
    assert me.status_code == 200
    assert me.json()["email"] == "ada@example.com"


async def test_the_endpoint_still_accepts_the_documented_body_field(client, entra):
    """docs/API.md documents `{id_token}`; the name is legacy, the checks are not."""
    response = await client.post("/api/auth/entra", json={"id_token": mint()})
    assert response.status_code == 200, response.text


async def test_the_endpoint_rejects_an_actual_id_token(client, entra):
    """The old flow's token: audienced to the client, and carrying no `scp`."""
    response = await client.post(
        "/api/auth/entra", json={"id_token": mint(aud=CLIENT_APP_ID, scp=None)}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_the_endpoint_reveals_nothing_about_which_check_failed(client, entra):
    bodies = set()
    for token in (
        mint(aud=CLIENT_APP_ID),
        mint(iss="https://login.microsoftonline.com/other/v2.0"),
        mint(exp_offset=-3600),
        mint(key=_IMPOSTOR_KEY),
        mint(scp=None),
        mint(scp="User.Read"),
    ):
        response = await client.post("/api/auth/entra", json={"id_token": token})
        assert response.status_code == 401
        bodies.add(response.json()["detail"])
    assert len(bodies) == 1, f"the 401 body distinguishes failures: {bodies}"


async def test_signing_in_twice_reuses_the_row_keyed_on_oid(client, entra, db):
    from sqlalchemy import func, select

    from app.models import User

    oid = "33333333-3333-3333-3333-333333333333"
    first = await client.post("/api/auth/entra", json={"id_token": mint(oid=oid)})
    second = await client.post("/api/auth/entra", json={"id_token": mint(oid=oid)})
    assert first.json()["user"]["id"] == second.json()["user"]["id"]

    count = await db.execute(select(func.count()).select_from(User).where(User.entra_oid == oid))
    assert count.scalar_one() == 1


async def test_a_changed_email_follows_the_oid_rather_than_forking_the_account(client, entra, db):
    """Email is mutable; `oid` is not. A rename must not create a second user."""
    oid = "44444444-4444-4444-4444-444444444444"
    first = await client.post("/api/auth/entra", json={"id_token": mint(oid=oid)})
    renamed = mint(oid=oid, extra={"preferred_username": "ada.byron@example.com"})
    second = await client.post("/api/auth/entra", json={"id_token": renamed})

    assert first.json()["user"]["id"] == second.json()["user"]["id"]
    assert second.json()["user"]["email"] == "ada.byron@example.com"


async def test_an_account_with_no_entra_identity_yet_is_adopted_by_email(
    client, entra, db, make_user
):
    """The migration path: a local row created before Entra keeps its data."""
    legacy = await make_user(email="grace@example.com")
    assert legacy.entra_oid is None

    oid = "55555555-5555-5555-5555-555555555555"
    token = mint(oid=oid, extra={"preferred_username": "grace@example.com"})
    response = await client.post("/api/auth/entra", json={"id_token": token})

    assert response.status_code == 200, response.text
    assert response.json()["user"]["id"] == str(legacy.id)
    await db.refresh(legacy)
    assert legacy.entra_oid == oid


async def test_a_recycled_address_cannot_take_over_an_account_bound_to_another_oid(
    client, entra, db, make_user
):
    """Only an account with no Entra identity yet may be adopted by email.

    Without this, anyone the directory issues a recycled `preferred_username` to
    inherits the previous holder's threads, bots and approvals.
    """
    incumbent = await make_user(email="shared@example.com")
    incumbent.entra_oid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await db.commit()
    incumbent_id = str(incumbent.id)

    intruder_oid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    token = mint(oid=intruder_oid, extra={"preferred_username": "shared@example.com"})
    response = await client.post("/api/auth/entra", json={"id_token": token})

    assert response.status_code == 200, response.text
    assert response.json()["user"]["id"] != incumbent_id
    # The address was not ours to take, so the new row gets a synthetic one.
    assert response.json()["user"]["email"] == f"{intruder_oid}@entra.local"

    await db.refresh(incumbent)
    assert incumbent.entra_oid == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert incumbent.email == "shared@example.com"
