"""Secret resolution — Key Vault refs, with an env fallback for local dev.

Resolved values never enter a log line, an API response, or an audit event.
Callers get the plaintext in-process only; everything that leaves this module
by another route carries the *reference*, never the value.

This module also *writes* one kind of secret: a connector credential typed
into the app (`store_connector_secret`). Key Vault is preferred and the row
still only ever holds a reference; when the deployment's identity cannot write
to the vault — today's does not, it holds "Key Vault Secrets User", which is
read-only — the value falls back to the Fernet-at-rest mechanism
`provider_credentials.py` already established for app-typed provider keys, and
the caller is told which backend took it. Nothing here relaxes the rule above:
the *value* never travels, in either direction, by any route but a return
value.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from typing import Any, Literal, NamedTuple
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import BotConnector
from app.services import provider_credentials

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
ENV_SCHEME = "env://"
KV_SCHEME = "kv://"
#: A value the app itself stored, encrypted at rest in Postgres because the
#: vault refused the write (or there is no vault). `app://connector/{bot_id}/
#: {connector_id}` is a *marker*, not a secret: it is safe in `secret_ref`, in
#: `GET /bots/{id}/connectors`, and in a log line, exactly like `kv://…` is.
APP_SCHEME = "app://"

# ref -> (fetched_at_monotonic, value). Values are secrets: never log this map.
_cache: dict[str, tuple[float, str]] = {}
_cache_lock: asyncio.Lock | None = None
_clients: dict[str, Any] = {}
_credential: Any | None = None


def _lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def reset_cache() -> None:
    """Drop cached secrets and clients (used on rotation and by tests)."""
    _cache.clear()
    _clients.clear()
    global _credential
    _credential = None


def _cache_key(parsed: tuple[str, str, str]) -> str:
    kind, location, name = parsed
    return f"{kind}:{location}:{name}"


def forget(ref: str) -> None:
    """Drop one cached value, by reference.

    `store_connector_secret` replaces a credential *under the same reference*
    — that is the design (`docs/security.md`: "Rotation is a new Key Vault
    version. `secret_ref` does not change and no bot needs rebinding"). With a
    `CACHE_TTL_SECONDS`-long cache keyed on the reference rather than the
    version, the first thing anyone does after replacing a credential — run the
    connector to see whether it worked — would be served the old value.

    `reset_cache()` would also fix that, and would additionally throw away
    every *other* bot's cached credential and every vault client, punishing the
    whole process for one replace. This forgets exactly the one key.

    Other replicas still converge on their own `CACHE_TTL_SECONDS`, the same
    bound `provider_credentials.RELOAD_INTERVAL_SECONDS` already accepts.
    """
    parsed = parse_ref(ref)
    if parsed is None:
        return
    _cache.pop(_cache_key(parsed), None)


def _vault_url(vault_name: str) -> str:
    return f"https://{vault_name}.vault.azure.net"


def parse_ref(ref: str) -> tuple[str, str, str] | None:
    """Split a secret ref into `(kind, location, name)`.

    Accepted forms:
      * `kv://{vault-name}/{secret-name}`  -> ("kv", vault_url, secret_name)
      * `env://VAR_NAME`                   -> ("env", "", "VAR_NAME")
      * `app://connector/{bot}/{conn}`     -> ("app", "", "connector/{bot}/{conn}")
      * `secret-name`                      -> ("kv", AZURE_KEY_VAULT_URL, name)

    `app://` is written only by `store_connector_secret`, never typed by a
    person — but it has to parse here anyway, because `bind_connector`'s guard
    refuses anything `parse_ref` rejects, and re-binding a connector whose
    credential was app-encrypted would otherwise 422 on the app's own marker.
    """
    value = (ref or "").strip()
    if not value:
        return None

    if value.lower().startswith(APP_SCHEME):
        name = value[len(APP_SCHEME) :].strip().strip("/")
        return ("app", "", name) if name else None

    if value.lower().startswith(ENV_SCHEME):
        name = value[len(ENV_SCHEME) :].strip()
        return ("env", "", name) if name else None

    if value.lower().startswith(KV_SCHEME):
        remainder = value[len(KV_SCHEME) :].strip("/")
        if "/" not in remainder:
            return None
        vault, _, name = remainder.partition("/")
        vault, name = vault.strip(), name.strip()
        if not vault or not name:
            return None
        # Accept a bare vault name or a full hostname.
        location = vault if vault.startswith("http") else _vault_url(vault.split(".")[0])
        return ("kv", location, name)

    # Bare name against the configured default vault.
    default_vault = (get_settings().azure_key_vault_url or "").strip().rstrip("/")
    if not default_vault:
        return None
    if "/" in value or urlparse(value).scheme:
        return None
    return ("kv", default_vault, value)


def _get_credential() -> Any | None:
    """Lazily build DefaultAzureCredential; None when azure-identity is absent."""
    global _credential
    if _credential is not None:
        return _credential
    try:
        from azure.identity.aio import DefaultAzureCredential

        _credential = DefaultAzureCredential()
    except Exception as exc:  # noqa: BLE001 - no azure libs / no managed identity
        logger.info("azure credential unavailable (%s) — secret refs will not resolve", exc)
        return None
    return _credential


def _get_client(vault_url: str) -> Any | None:
    client = _clients.get(vault_url)
    if client is not None:
        return client
    credential = _get_credential()
    if credential is None:
        return None
    try:
        from azure.keyvault.secrets.aio import SecretClient

        client = SecretClient(vault_url=vault_url, credential=credential)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not build Key Vault client for %s: %s", vault_url, exc)
        return None
    _clients[vault_url] = client
    return client


async def resolve_secret(ref: str, db: AsyncSession | None = None) -> str | None:
    """Resolve a secret reference to its value, or None when unresolvable.

    Never raises and never logs the value — only the reference.

    `db` is needed only by `app://` refs, whose value lives in a table rather
    than in a vault or the environment. It is optional because most callers
    (`inbound.py`'s signing-key lookup, for one) resolve refs a person typed,
    and a person can never type an `app://` ref — only
    `store_connector_secret` writes one.
    """
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    kind, location, name = parsed
    cache_key = _cache_key(parsed)

    cached = _cache.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    async with _lock():
        cached = _cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

        value: str | None = None
        if kind == "env":
            value = os.getenv(name) or None
        elif kind == "app":
            if db is None:
                logger.info(
                    "secret ref %r is app-stored and this caller has no db session",
                    _redact_ref(ref),
                )
                return None
            value = await provider_credentials.get_app_secret(db, key=name)
        else:
            client = _get_client(location)
            if client is None:
                return None
            try:
                secret = await client.get_secret(name)
                value = secret.value
            except Exception as exc:  # noqa: BLE001 - missing secret / no access
                logger.warning("secret ref %r did not resolve: %s", _redact_ref(ref), exc)
                return None

        if value is None:
            logger.info("secret ref %r resolved to nothing", _redact_ref(ref))
            return None
        _cache[cache_key] = (time.monotonic(), value)
        return value


def _redact_ref(ref: str) -> str:
    """Refs are not secrets, but keep log lines short and predictable."""
    return (ref or "")[:120]


#: What `check_ref` concluded. `reason` is "" when the ref is good, and
#: otherwise names which of the three failures it was, because the three need
#: different sentences from the caller: a person who typed nonsense, a person
#: who pasted a credential into the wrong box, and a vault that is not
#: answering right now are not the same problem.
class RefCheck(NamedTuple):
    ok: bool
    reason: str  # "" | "unparsable" | "missing" | "unverifiable"


async def check_ref(ref: str, db: AsyncSession | None = None) -> RefCheck:
    """Does this reference actually name something that exists?

    The guard that made this necessary, and why shape rules could not do it.
    `parse_ref`'s last branch accepts a bare name against the default vault, so
    once `AZURE_KEY_VAULT_URL` is set — every real deployment — these two are
    indistinguishable by inspection:

        crm-key-in-the-default-vault          a reference somebody means to use
        sk-live-51H8xQ2eZvKYlo0hunter2        a live credential pasted into the
                                              only box the app used to offer

    Both are legal Key Vault secret names (`^[0-9a-zA-Z-]{1,127}$`), so no
    charset test separates them, and separating them by prefix or entropy is a
    guess that will one day refuse a name somebody legitimately chose. The
    consequence of guessing wrong in the permissive direction is the one that
    matters: `secret_ref` is echoed by `GET /bots/{id}/connectors` to every user
    who can see the bot, so a pasted key is a credential published to the
    tenant and left in a column forever.

    So this asks the vault instead of guessing. A name that exists is a
    reference; a name that does not is either a typo or a pasted secret, and in
    both cases the caller should not store it. That is a network call, which is
    why it is deliberately **not** in `parse_ref` — parsing stays pure and is
    used on the resolve path — and why it is only called where a *person* is
    supplying a ref: binding a connector, and configuring an inbound source.

    Reads the secret to test for it because Key Vault has no cheaper existence
    check. The value is discarded here and never logged; `resolve_secret`'s
    cache is not populated either, because a ref being checked is not yet a ref
    in use.

    Never raises. "unverifiable" is a real answer — no Azure credential, or the
    vault refusing to answer — and callers are expected to treat it as a
    refusal rather than a pass, because failing open here re-opens exactly the
    leak this exists to close.
    """
    parsed = parse_ref(ref)
    if parsed is None:
        return RefCheck(False, "unparsable")
    kind, location, name = parsed

    if kind == "env":
        return RefCheck(True, "") if os.getenv(name) else RefCheck(False, "missing")

    if kind == "app":
        # Only `store_connector_secret` writes this scheme, and re-binding a
        # connector sends the app's own marker straight back. With a session in
        # hand the row is checked; without one the marker is taken at face
        # value, because a person cannot type one of these.
        if db is None:
            return RefCheck(True, "")
        stored = await provider_credentials.get_app_secret(db, key=name)
        return RefCheck(True, "") if stored else RefCheck(False, "missing")

    client = _get_client(location)
    if client is None:
        return RefCheck(False, "unverifiable")
    try:
        await client.get_secret(name)
    except Exception as exc:  # noqa: BLE001 - missing secret, or no access at all
        logger.info("secret ref %r did not check out: %s", _redact_ref(ref), exc)
        return RefCheck(False, "missing" if _looks_absent(exc) else "unverifiable")
    return RefCheck(True, "")


def _looks_absent(exc: Exception) -> bool:
    """Is this "there is no such secret" rather than "I could not ask"?

    Key Vault answers a missing secret with `ResourceNotFoundError` (a 404),
    and everything else — a 403 from a missing role, a timeout, DNS — has to
    read as unverifiable so the caller fails closed. Matched on the class name
    and the status code rather than by importing azure.core, which is optional
    at runtime; `LookupError` is in the list because that is what the test
    double raises for an absent name.
    """
    if isinstance(exc, LookupError):
        return True
    if type(exc).__name__ in ("ResourceNotFoundError", "SecretNotFound"):
        return True
    return getattr(exc, "status_code", None) == 404


async def resolve_connector_secrets(
    db: AsyncSession,
    bot_id,
    connector_id: str,
) -> dict[str, str]:
    """Resolve the secrets bound to one bot/connector pair.

    Returns `{}` when nothing is bound or nothing resolves, so callers can
    degrade to the mock path. The returned dict is for in-process use only —
    it must never be attached to a response, audit event, or log line.
    """
    result = await db.execute(
        select(BotConnector).where(
            BotConnector.bot_id == bot_id,
            BotConnector.connector_id == connector_id,
        )
    )
    link = result.scalar_one_or_none()
    if link is None or not link.secret_ref:
        return {}

    value = await resolve_secret(link.secret_ref, db)
    if value is None:
        return {}
    return {"secret": value}


# ---------------------------------------------------------------------------
# Writing a credential the app was given
# ---------------------------------------------------------------------------

#: Which store actually took the value. Returned to the caller and shown in the
#: UI, because the alternative — saying "stored" and letting someone assume
#: Key Vault — is how a credential ends up in a place its owner did not agree
#: to. `key_vault` is preferred; `app_encrypted` is honest degradation.
SecretBackend = Literal["key_vault", "app_encrypted", "env", "none"]


class StoredSecret(NamedTuple):
    backend: SecretBackend
    secret_ref: str
    detail: str


def describe_backend(ref: str | None) -> SecretBackend:
    """Which store a binding's reference points at, from the ref alone.

    Derived rather than recorded, so it survives a reload and cannot disagree
    with the reference beside it: `bot_connectors` has one column and it holds
    the ref (adding a second would let the two drift after a hand-edited row or
    an old client's `bind`).
    """
    parsed = parse_ref(ref or "")
    if parsed is None:
        return "none"
    kind = parsed[0]
    if kind == "kv":
        return "key_vault"
    if kind == "app":
        return "app_encrypted"
    return "env"


def connector_secret_name(bot_id: uuid.UUID | str, connector_id: str) -> str:
    """The Key Vault secret name for one bot/connector pair.

    Key Vault names accept `[0-9a-zA-Z-]` and nothing else, up to 127
    characters — a connector id is snake_case (`microsoft_graph`), so the
    underscores have to go. Replacing them is not injective on its own: a
    connector registered as `a-b` and one registered as `a_b` would collapse
    onto the same vault secret and silently overwrite each other's credential
    (`POST /integrations/connectors` does not constrain the id server-side —
    only `IntegrationsPanel.validateManifest` does, client-side). The hash of
    the exact pair, appended, is what makes the name unique; the readable slug
    in front of it is so a person looking at the vault can tell what it is.
    """
    pair = f"{bot_id}:{connector_id}"
    digest = hashlib.sha256(pair.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^0-9a-zA-Z-]", "-", pair)[:100]
    return f"nesq-conn-{slug}-{digest}"


def _write_failure_reason(exc: Exception) -> str:
    """Why a vault write failed, in words that cannot contain the value.

    Deliberately not `str(exc)`. An Azure `HttpResponseError` renders the
    service's response body, and this module's contract on secret values is
    absolute — it is not worth reasoning about whether some 400 from
    `set_secret` quotes back the thing we sent it. The exception class and the
    HTTP status are enough to tell a permission problem (403) from a missing
    vault (404) from a network failure, which is the whole diagnostic question.
    """
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return (
            "Key Vault refused the write (HTTP 403) — the API's managed identity "
            "has read access only. Grant it the 'Key Vault Secrets Officer' role "
            "on the vault to store connector credentials there."
        )
    if status:
        return f"the Key Vault write failed ({type(exc).__name__}, HTTP {status})"
    return f"the Key Vault write failed ({type(exc).__name__})"


async def store_connector_secret(
    db: AsyncSession,
    *,
    bot_id: uuid.UUID,
    connector_id: str,
    value: str,
    user_id: uuid.UUID | None = None,
) -> StoredSecret:
    """Take a credential value and put it somewhere the app only references.

    Key Vault first. When the vault refuses — and today it does, because the
    deployed user-assigned identity holds "Key Vault Secrets User", which
    cannot write (`infra/azure/main.bicep`) — the value goes to the same
    at-rest mechanism `provider_credentials.py` chose for app-typed provider
    keys: a Fernet token in Postgres, keyed off `JWT_SECRET`. That decision is
    not re-made here and no second crypto scheme is introduced; this calls
    straight into that module.

    The returned `backend` is the point of the whole function. A caller that
    cannot tell Key Vault from Postgres will tell its user the wrong thing
    about where their credential went.

    Never raises on a vault failure: refusing the request would leave the
    person with no way to store a credential at all on a deployment whose
    identity is read-only, which is every deployment today.
    """
    vault_url = (get_settings().azure_key_vault_url or "").strip().rstrip("/")
    reason = "no Key Vault is configured (AZURE_KEY_VAULT_URL is unset)"

    if vault_url:
        name = connector_secret_name(bot_id, connector_id)
        client = _get_client(vault_url)
        if client is None:
            reason = "no Key Vault client could be built (no Azure credential in this process)"
        else:
            try:
                await client.set_secret(name, value)
            except Exception as exc:  # noqa: BLE001 - every failure degrades, see docstring
                reason = _write_failure_reason(exc)
                logger.warning("connector credential could not be written to Key Vault: %s", reason)
            else:
                host = urlparse(vault_url).hostname or vault_url
                ref = f"{KV_SCHEME}{host}/{name}"
                # The reference is unchanged by a replace (that is the design),
                # so the resolver's cache has to be told, or the next run reads
                # the credential this one just replaced.
                forget(ref)
                return StoredSecret(
                    "key_vault",
                    ref,
                    f"Stored in Key Vault as {name}. Nesq Bot keeps only the reference.",
                )

    ref = f"{APP_SCHEME}connector/{bot_id}/{connector_id}"
    await provider_credentials.set_app_secret(
        db,
        key=f"connector/{bot_id}/{connector_id}",
        value=value,
        user_id=user_id,
    )
    forget(ref)
    return StoredSecret(
        "app_encrypted",
        ref,
        f"Encrypted in this deployment's database, not Key Vault: {reason}",
    )
