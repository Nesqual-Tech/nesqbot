"""`ModelRouter.client()` auth-mode selection: api_key / managed_identity / mock.

Production ships with an Azure OpenAI endpoint and **no key** — it authenticates
with the container's user-assigned managed identity. Before that was supported,
``client()`` returned ``None`` whenever the key was blank, so every deployed bot
replied ``[mock:mini] …``. These tests pin all three outcomes. The mock one
matters as much as the new one: keyless local dev depends on it staying
reachable, and the whole suite runs in that configuration.

Nothing here touches IMDS — ``azure.identity.aio`` is patched in place, which
also lets us assert the one detail that fails confusingly at runtime if it is
wrong: the identity's client id must be passed explicitly.
"""

from __future__ import annotations

import ast
import inspect
import logging
from typing import Any

import pytest

from app.config import Settings
from app.services.model_router import (
    _LOGGED_AUTH_MODES,
    _TOKEN_PROVIDERS,
    AZURE_OPENAI_SCOPE,
    ModelRouter,
)

#: The real values from the production container app, so the test reads like the
#: deployment it protects. `AZURE_CLIENT_ID` is the Entra API app registration;
#: `AZURE_MANAGED_IDENTITY_CLIENT_ID` is the user-assigned identity.
ENTRA_API_APP_ID = "20000000-0000-0000-0000-000000000002"
UAMI_CLIENT_ID = "50000000-0000-0000-0000-000000000005"
ENDPOINT = "https://your-aoai.openai.azure.com/"


def _settings(**overrides: Any) -> Settings:
    """Settings with every Azure knob explicitly off unless a test turns it on."""
    base: dict[str, Any] = {
        "azure_openai_endpoint": "",
        "azure_openai_api_key": "",
        "azure_managed_identity_client_id": "",
    }
    return Settings(**{**base, **overrides})


class _FakeCredential:
    """Records how the credential was constructed. Never reaches the network."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _clean_router_globals():
    """Token providers and the log-once ledger are process-global by design."""
    _TOKEN_PROVIDERS.clear()
    _LOGGED_AUTH_MODES.clear()
    yield
    _TOKEN_PROVIDERS.clear()
    _LOGGED_AUTH_MODES.clear()


def _patch_identity(monkeypatch, module, seen: dict[str, Any], flavour: str) -> None:
    def fake_credential(*args: Any, **kwargs: Any) -> _FakeCredential:
        credential = _FakeCredential(*args, **kwargs)
        seen["credential"] = credential
        seen["flavour"] = flavour
        return credential

    def fake_provider(credential: Any, *scopes: str, **kwargs: Any):
        seen["credential_passed_to_provider"] = credential
        seen["scopes"] = scopes
        return lambda: "fake-entra-token"

    monkeypatch.setattr(module, "ManagedIdentityCredential", fake_credential)
    monkeypatch.setattr(module, "get_bearer_token_provider", fake_provider)


@pytest.fixture
def azure_identity_spy(monkeypatch):
    """Patch both `azure.identity` flavours; report what the router asked them for."""
    import azure.identity
    import azure.identity.aio

    seen: dict[str, Any] = {}
    _patch_identity(monkeypatch, azure.identity.aio, seen, "aio")
    _patch_identity(monkeypatch, azure.identity, seen, "sync")
    return seen


# ---------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------


def test_no_endpoint_and_no_key_selects_mock():
    router = ModelRouter(_settings())
    assert router.client() is None
    assert router.auth_mode == "mock"


def test_no_endpoint_selects_mock_even_when_a_key_is_present():
    """The endpoint is what there is to call; a stray key cannot conjure one."""
    router = ModelRouter(_settings(azure_openai_api_key="sk-local"))
    assert router.client() is None
    assert router.auth_mode == "mock"


def test_whitespace_only_configuration_is_treated_as_absent():
    router = ModelRouter(_settings(azure_openai_endpoint="   ", azure_openai_api_key="  "))
    assert router.client() is None
    assert router.auth_mode == "mock"


async def test_the_mock_reply_path_is_still_reachable():
    """The suite, and every keyless checkout, runs on this branch."""
    router = ModelRouter(_settings())
    result = await router.chat(task="agent_turn", messages=[{"role": "user", "content": "hi"}])
    assert result.content.startswith("[mock:mini]")
    assert router.auth_mode == "mock"


def test_a_credential_that_cannot_be_built_degrades_to_mock(monkeypatch):
    """No identity endpoint (a laptop with AZURE_OPENAI_ENDPOINT set) still boots."""
    import azure.identity
    import azure.identity.aio

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("no identity endpoint in this environment")

    monkeypatch.setattr(azure.identity.aio, "ManagedIdentityCredential", boom)
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", boom)
    router = ModelRouter(_settings(azure_openai_endpoint=ENDPOINT))
    assert router.client() is None
    assert router.auth_mode == "mock"


# ---------------------------------------------------------------------------
# api_key — unchanged behaviour for local dev
# ---------------------------------------------------------------------------


def test_endpoint_plus_key_selects_api_key_auth():
    router = ModelRouter(_settings(azure_openai_endpoint=ENDPOINT, azure_openai_api_key="sk-local"))
    client = router.client()
    assert client is not None
    assert router.auth_mode == "api_key"
    assert client.api_key == "sk-local"
    # No Entra provider is wired up on the key path.
    assert getattr(client, "_azure_ad_token_provider", None) is None


def test_a_key_wins_over_a_managed_identity():
    """An explicitly configured key is an explicit choice; do not second-guess it."""
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=ENDPOINT,
            azure_openai_api_key="sk-local",
            azure_managed_identity_client_id=UAMI_CLIENT_ID,
        )
    )
    assert router.client() is not None
    assert router.auth_mode == "api_key"


def test_the_client_is_built_once_and_cached():
    router = ModelRouter(_settings(azure_openai_endpoint=ENDPOINT, azure_openai_api_key="sk-local"))
    assert router.client() is router.client()


# ---------------------------------------------------------------------------
# managed_identity — the production path
# ---------------------------------------------------------------------------


def test_endpoint_without_a_key_selects_managed_identity(azure_identity_spy):
    router = ModelRouter(
        _settings(azure_openai_endpoint=ENDPOINT, azure_managed_identity_client_id=UAMI_CLIENT_ID)
    )
    client = router.client()
    assert client is not None
    assert router.auth_mode == "managed_identity"
    assert client._azure_ad_token_provider is not None
    assert not client.api_key  # no key was supplied; the bearer token carries the auth


def test_the_managed_identity_client_id_is_passed_explicitly(azure_identity_spy):
    """The trap: `AZURE_CLIENT_ID` is the Entra API app, not the identity.

    Handing that id to a managed-identity credential — which is exactly what a
    bare `DefaultAzureCredential` does, since it reads the ambient
    `AZURE_CLIENT_ID` — fails at IMDS, because the app registration has no
    managed identity. The router must pass `AZURE_MANAGED_IDENTITY_CLIENT_ID`
    as an explicit keyword instead.
    """
    router = ModelRouter(
        _settings(
            azure_openai_endpoint=ENDPOINT,
            azure_client_id=ENTRA_API_APP_ID,
            azure_managed_identity_client_id=UAMI_CLIENT_ID,
        )
    )
    assert router.client() is not None
    credential = azure_identity_spy["credential"]
    assert credential.args == ()
    assert credential.kwargs == {"client_id": UAMI_CLIENT_ID}
    assert ENTRA_API_APP_ID not in str(credential.kwargs)


def test_an_unset_identity_client_id_means_system_assigned_never_the_app_id(azure_identity_spy):
    router = ModelRouter(_settings(azure_openai_endpoint=ENDPOINT, azure_client_id=ENTRA_API_APP_ID))
    assert router.client() is not None
    assert router.auth_mode == "managed_identity"
    # `client_id=None` is "the system-assigned identity"; `""` would be looked up
    # as a user-assigned one, and the app id would be flatly wrong.
    assert azure_identity_spy["credential"].kwargs == {"client_id": None}


def test_the_async_credential_is_preferred(azure_identity_spy):
    """`azure.identity.aio` keeps the token fetch off the event loop."""
    router = ModelRouter(
        _settings(azure_openai_endpoint=ENDPOINT, azure_managed_identity_client_id=UAMI_CLIENT_ID)
    )
    assert router.client() is not None
    assert azure_identity_spy["flavour"] == "aio"


def test_a_missing_aiohttp_falls_back_to_the_sync_credential(monkeypatch, azure_identity_spy):
    """The regression that shipped v0.1.1 mocking.

    `azure.identity.aio.ManagedIdentityCredential` raises "aiohttp package is
    not installed" when the async transport is absent — azure-core only requires
    `requests`. Degrading to the blocking credential keeps production talking to
    Foundry; degrading to mock is what we are here to prevent.
    """
    import azure.identity.aio

    def no_aiohttp(*args: Any, **kwargs: Any):
        raise ImportError("aiohttp package is not installed")

    monkeypatch.setattr(azure.identity.aio, "ManagedIdentityCredential", no_aiohttp)
    router = ModelRouter(
        _settings(azure_openai_endpoint=ENDPOINT, azure_managed_identity_client_id=UAMI_CLIENT_ID)
    )
    assert router.client() is not None
    assert router.auth_mode == "managed_identity"
    assert azure_identity_spy["flavour"] == "sync"
    assert azure_identity_spy["credential"].kwargs == {"client_id": UAMI_CLIENT_ID}


def test_aiohttp_is_pinned_so_the_async_credential_can_be_built():
    """azure-identity does not pull it in; nothing else in the image does either."""
    from pathlib import Path

    requirements = (Path(__file__).resolve().parents[2] / "requirements.txt").read_text(encoding="utf-8")
    assert any(line.strip().startswith("aiohttp==") for line in requirements.splitlines())


def test_the_token_scope_is_the_data_plane_one_not_arm(azure_identity_spy):
    """An ARM-audienced token is rejected by Foundry with a confusing 401."""
    assert AZURE_OPENAI_SCOPE == "https://cognitiveservices.azure.com/.default"
    router = ModelRouter(
        _settings(azure_openai_endpoint=ENDPOINT, azure_managed_identity_client_id=UAMI_CLIENT_ID)
    )
    router.client()
    assert azure_identity_spy["scopes"] == (AZURE_OPENAI_SCOPE,)


def test_the_token_provider_is_shared_between_routers(azure_identity_spy):
    """One credential per identity: it holds the token cache all callers want."""
    settings = _settings(azure_openai_endpoint=ENDPOINT, azure_managed_identity_client_id=UAMI_CLIENT_ID)
    first = ModelRouter(settings).client()
    second = ModelRouter(settings).client()
    assert first is not second  # separate OpenAI clients …
    assert first._azure_ad_token_provider is second._azure_ad_token_provider  # … one provider


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------


def test_the_router_never_uses_default_azure_credential():
    from app.services import model_router as module

    source = inspect.getsource(module)
    assert "ManagedIdentityCredential" in source
    # Mentioned only in the comment explaining why it is not used.
    assert "DefaultAzureCredential(" not in source


def test_azure_identity_is_imported_lazily():
    """A mock-mode deployment must never load `azure.identity`."""
    from app.services import model_router as module

    tree = ast.parse(inspect.getsource(module))
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    modules = [n.module or "" for n in top_level if isinstance(n, ast.ImportFrom)]
    modules += [alias.name for n in top_level if isinstance(n, ast.Import) for alias in n.names]
    assert not [m for m in modules if m.startswith("azure")]


def test_settings_expose_the_managed_identity_client_id():
    assert "azure_managed_identity_client_id" in Settings.model_fields
    assert Settings.model_fields["azure_managed_identity_client_id"].default == ""


def test_rag_embeddings_share_the_routers_client():
    """`rag.embed()` has no Azure client of its own — one auth decision, one place."""
    from app.services import rag

    assert isinstance(rag._router, ModelRouter)
    source = inspect.getsource(rag)
    assert "_router.client()" in source
    assert "AsyncAzureOpenAI" not in source
    assert "azure.identity" not in source


# ---------------------------------------------------------------------------
# Logging — "why is it mocking?" must be answerable without a redeploy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "mock"),
        ({"azure_openai_endpoint": ENDPOINT, "azure_openai_api_key": "sk-local"}, "api_key"),
        ({"azure_openai_endpoint": ENDPOINT, "azure_managed_identity_client_id": UAMI_CLIENT_ID}, "managed_identity"),
    ],
)
def test_the_selected_auth_mode_is_logged_once_at_info(caplog, azure_identity_spy, overrides, expected):
    with caplog.at_level(logging.INFO, logger="app.services.model_router"):
        ModelRouter(_settings(**overrides)).client()
        # A second router in the same process must not repeat the line.
        ModelRouter(_settings(**overrides)).client()

    records = [r for r in caplog.records if "auth mode=" in r.getMessage()]
    assert len(records) == 1
    assert f"auth mode={expected}" in records[0].getMessage()
    assert records[0].levelno == logging.INFO


def test_configure_logging_gives_the_app_loggers_a_handler():
    """Without this the auth-mode line is written into a logger with no handler.

    uvicorn configures only its own loggers, so an unconfigured root means every
    `app.*` INFO record is silently dropped in the container — which is exactly
    how "why is it mocking?" became unanswerable from the logs.
    """
    from app.config import configure_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers.clear()
        configure_logging(_settings(log_level="INFO"))
        assert root.handlers, "configure_logging left the root logger without a handler"
        assert root.level == logging.INFO
        assert logging.getLogger("azure.core").level == logging.WARNING
    finally:
        root.handlers[:] = saved
        root.setLevel(saved_level)


def test_configure_logging_never_overrides_an_existing_setup():
    """pytest's capture plugin, a --log-config, an embedding host: leave them be."""
    from app.config import configure_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        sentinel = logging.NullHandler()
        root.handlers[:] = [sentinel]
        configure_logging(_settings(log_level="DEBUG"))
        assert root.handlers == [sentinel]
    finally:
        root.handlers[:] = saved


def test_the_mock_log_line_says_why(caplog):
    with caplog.at_level(logging.INFO, logger="app.services.model_router"):
        ModelRouter(_settings()).client()
    message = next(r.getMessage() for r in caplog.records if "auth mode=" in r.getMessage())
    assert "AZURE_OPENAI_ENDPOINT" in message


def test_the_managed_identity_log_line_names_the_identity(caplog, azure_identity_spy):
    with caplog.at_level(logging.INFO, logger="app.services.model_router"):
        ModelRouter(
            _settings(azure_openai_endpoint=ENDPOINT, azure_managed_identity_client_id=UAMI_CLIENT_ID)
        ).client()
    message = next(r.getMessage() for r in caplog.records if "auth mode=" in r.getMessage())
    assert UAMI_CLIENT_ID in message


def test_the_api_key_log_line_does_not_leak_the_key(caplog):
    with caplog.at_level(logging.INFO, logger="app.services.model_router"):
        ModelRouter(_settings(azure_openai_endpoint=ENDPOINT, azure_openai_api_key="sk-super-secret")).client()
    message = next(r.getMessage() for r in caplog.records if "auth mode=" in r.getMessage())
    assert "sk-super-secret" not in message
