"""Application settings loaded from environment / .env."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    nesq_env: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    database_url: str = "postgresql+asyncpg://nesq:nesq@localhost:5432/nesqbot"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-change-me"

    # Shared secret the Temporal worker presents as a bearer token. The worker
    # and the bicep have always set WORKER_API_TOKEN; the API simply never read
    # it, so every worker call 401'd and no scheduled routine could fire. Empty
    # disables the service-token path entirely - an unset value must never mean
    # "accept anything".
    worker_api_token: str = ""
    cors_origins: str = "http://localhost:1420,http://localhost:8081"
    # Level for the application's own loggers. See `configure_logging`: below
    # WARNING these records do not reach the container log at all.
    log_level: str = "INFO"

    azure_tenant_id: str = ""
    # THE API'S OWN app registration id — the `aud` this API accepts, *not* the
    # desktop/mobile client's id. See docs/entra-setup.md: the resource server is
    # `Nesq Bot API` (20000000-…) and the public client is `Nesq Bot` (30000000-…).
    # Setting the client's id here would make the API accept tokens audienced to
    # the client — exactly the audience confusion the two-registration split
    # exists to prevent — and it would do so silently, because such tokens verify
    # against the same tenant JWKS. If sign-in starts failing with a valid-looking
    # token, check this value first.
    azure_client_id: str = ""
    azure_client_secret: str = ""
    # The USER-ASSIGNED MANAGED IDENTITY's client id — never the app registration
    # above. `AZURE_CLIENT_ID` is overloaded (see the comment on it and
    # infra/azure/README.md): once sign-in is wired up it holds the Entra API app
    # id, which `ManagedIdentityCredential` must never be handed. Anything
    # authenticating *as the container* — the ACI desktop driver, and the Azure
    # OpenAI client in services/model_router.py — must read this one, and must
    # pass it explicitly rather than relying on `DefaultAzureCredential` picking
    # up the ambient `AZURE_CLIENT_ID`, which would try to authenticate as an app
    # registration that has no managed identity and fail at IMDS.
    azure_managed_identity_client_id: str = ""
    # The delegated scope the client must have been granted, as it appears in the
    # access token's `scp` claim (short name, not the full `api://…/` URI).
    azure_api_scope: str = "access_as_user"

    azure_openai_endpoint: str = ""
    # Optional. Production deliberately ships without a key and authenticates
    # with the user-assigned managed identity instead; set this only for local
    # dev against a real Foundry account. See ModelRouter.client(): endpoint +
    # key -> api_key auth, endpoint alone -> managed identity, neither -> mock.
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-12-01-preview"
    # Deployment names, not model names - they must match the deployments the
    # bicep creates. Tier -> model mapping and why, as of 2026-08 (per 1M in/out):
    #   nano   gpt-5.6-luna  $0.20/$1.20  classify, route, compact - most calls
    #   mini   gpt-5.4-mini  $0.75/$4.50  agent turns and tool calling
    #   reason gpt-5.6-sol   $5.00/$30.00 rare escalations only
    # Keep TIER_PRICES in services/model_router.py in step with whatever is
    # deployed here; packages/model-router mirrors it and a test asserts parity.
    azure_deployment_nano: str = "gpt-5.6-luna"
    azure_deployment_mini: str = "gpt-5.4-mini"
    azure_deployment_reason: str = "gpt-5.6-sol"

    #: A second deployment for the reason tier, tried when the first is
    #: throttled and before the tier is given up on entirely.
    #:
    #: This exists because of an arithmetic problem, not a preference. Azure
    #: allocates token-per-minute quota *per model*, and the primary reason
    #: deployment holds its model's entire regional allowance:
    #:
    #:   grok-4-1-fast-reasoning   50 of a limit of 50   (= 50,000 TPM)
    #:
    #: so it cannot be raised. A different xAI model has its own untouched 50,
    #: which is the only way to buy reasoning throughput today without waiting
    #: on a quota request. Empty disables the hop and the tier falls straight
    #: back to `mini`.
    azure_deployment_reason_alt: str = ""
    azure_deployment_embed: str = "text-embedding-3-small"

    # ---- a second account, and per-tier endpoints ---------------------------
    #
    # The measured problem this exists for: the `reason` tier is 96% of the
    # bill and is priced at $5.00 per 1M input tokens, while `grok-4-1-fast-
    # reasoning` is $0.20 — 25x cheaper on exactly the thing this product
    # spends money on. It cannot simply be named in `azure_deployment_reason`,
    # because an xAI model **cannot be deployed to an `OpenAI`-kind Azure
    # account at all**. It lives on a separate `AIServices` account with its
    # own hostname, and one endpoint for the whole router is what stopped it
    # being reachable.
    #
    # So each tier may name its own endpoint. Blank means "use
    # `azure_openai_endpoint`", which is what every tier does today — the
    # default is deliberately unchanged and switching one is a config decision,
    # not a code one.
    #
    # Three rules that are easy to get wrong and are enforced in
    # `ModelRouter._endpoint_for`:
    #
    # * **A key belongs to an account, not to the router.** A tier pointed at
    #   an overridden endpoint never inherits `azure_openai_api_key`: presenting
    #   one account's key to another is a 401 that reads like a broken
    #   deployment. Set `AZURE_OPENAI_API_KEY_<TIER>` or leave it keyless.
    # * **Managed identity is the production path either way.** Both accounts
    #   take an Entra token for `https://cognitiveservices.azure.com/.default`,
    #   and the API's user-assigned identity already holds
    #   `Cognitive Services OpenAI User` on both. It must be built with an
    #   explicit `client_id` — see `azure_managed_identity_client_id`.
    # * **`TIER_PRICES` does not follow the endpoint.** Pointing `reason` at a
    #   Grok deployment does not make the ledger bill Grok's price; that table
    #   is edited by hand in `services/model_router.py` *and* mirrored into
    #   `packages/model-router/src/index.ts`. The router logs a warning when a
    #   tier is overridden so an unbilled switch cannot happen quietly.
    #
    # Measured against the live xAI account on 2026-08-23 (see
    # `tests/services/test_model_router_endpoints.py`): the classic
    # `/openai/deployments/{name}/chat/completions` route answers 200, so
    # `AsyncAzureOpenAI` needs no special casing; tools, streaming with
    # `include_usage`, and vision all work. What differs is priced, not shaped
    # — see `ChatResult` output accounting in `model_router`.
    #
    # The account, as it actually is, so nobody has to re-measure it:
    #
    #   endpoint   https://nesqbot-xai-CHANGE_ME.cognitiveservices.azure.com/
    #   kind       AIServices (an xAI model cannot deploy to an OpenAI-kind one)
    #   region     swedencentral, GlobalStandard
    #   rate limit 50,000 tokens AND 50 requests per 60 seconds, read off
    #              `x-ratelimit-limit-tokens` / `-requests` and their
    #              `-renewalperiod-*` headers. NOT 50 TPM: the deployment's
    #              `sku.capacity: 50` is thousands.
    #
    # Azure list price per 1M (retail prices API, 2026-08-23), against the
    # current reason tier's $5.00 / $30.00:
    #
    #   grok-4-1-fast-reasoning   $0.20 in / $0.50 out
    #   grok-4.3                  $1.25 in / $2.50 out ($0.20 cached in)
    #
    # None of that is in `TIER_PRICES` and it must not be until a tier is
    # actually switched — that table is mirrored into
    # `packages/model-router/src/index.ts` and a test asserts the two match.
    azure_openai_endpoint_nano: str = ""
    azure_openai_endpoint_mini: str = ""
    azure_openai_endpoint_reason: str = ""
    azure_openai_endpoint_embed: str = ""
    azure_openai_api_key_nano: str = ""
    azure_openai_api_key_mini: str = ""
    azure_openai_api_key_reason: str = ""
    azure_openai_api_key_embed: str = ""
    # Blank means "whatever `azure_openai_api_version` says". The xAI account
    # accepts the same `2024-12-01-preview`, so this is here for the next
    # account rather than for that one.
    azure_openai_api_version_nano: str = ""
    azure_openai_api_version_mini: str = ""
    azure_openai_api_version_reason: str = ""
    azure_openai_api_version_embed: str = ""

    # ---- OpenAI-protocol providers -------------------------------------------
    #
    # `AsyncOpenAI` and `AsyncAzureOpenAI` (same `openai` package, already a
    # dependency) return the identical response shape, so everything downstream
    # of ModelRouter.client() - _request_kwargs, parse_tool_calls,
    # billable_output_tokens, the streaming delta accumulator - needs no per-
    # provider branch. Only client construction and the model-name lookup
    # differ, which is what this block and ModelRouter._openai_client() cover.
    #
    # This one client shape also covers a self-hosted "local model" server -
    # Ollama, vLLM, LM Studio, OpenRouter, or anything else that speaks the
    # OpenAI chat-completions wire format - by pointing `openai_base_url` at it.
    # There is no separate "local" code path: it is this same client with a
    # different base_url, most such servers accept any non-empty API key.
    #
    # `model_provider` picks the provider per tier; blank means "azure", which
    # keeps every existing deployment's behaviour byte-for-byte unchanged.
    model_provider: str = "azure"
    model_provider_nano: str = ""
    model_provider_mini: str = ""
    model_provider_reason: str = ""
    model_provider_embed: str = ""

    # Blank base_url means the SDK's own default (https://api.openai.com/v1) -
    # real OpenAI. A self-hosted server sets this to its own address instead,
    # globally or per tier.
    openai_base_url: str = ""
    openai_base_url_nano: str = ""
    openai_base_url_mini: str = ""
    openai_base_url_reason: str = ""
    openai_base_url_embed: str = ""
    openai_api_key: str = ""
    openai_api_key_nano: str = ""
    openai_api_key_mini: str = ""
    openai_api_key_reason: str = ""
    openai_api_key_embed: str = ""
    # Real model names, not Azure deployment aliases - e.g. "gpt-5.1",
    # "llama3.1:70b". No default: a tier routed to this provider with no model
    # name configured cannot be resolved and falls back to mock, the same as an
    # Azure tier with no endpoint.
    openai_model_nano: str = ""
    openai_model_mini: str = ""
    openai_model_reason: str = ""
    openai_model_embed: str = ""

    # ---- Anthropic ------------------------------------------------------------
    #
    # One fixed endpoint (api.anthropic.com), so unlike the openai block above
    # there is no base_url. Everything else mirrors it: a shared key/model with
    # per-tier overrides, translated to and from the OpenAI wire shape by
    # ModelRouter._anthropic_client() / the adapter classes next to it, so nothing
    # downstream of client() needs a third branch.
    anthropic_api_key: str = ""
    anthropic_api_key_nano: str = ""
    anthropic_api_key_mini: str = ""
    anthropic_api_key_reason: str = ""
    anthropic_api_key_embed: str = ""
    # Real model names - e.g. "claude-opus-4-5-20251101". No default, same
    # reasoning as openai_model_*: an unconfigured tier cannot be resolved and
    # falls back to mock rather than guessing at a model name.
    anthropic_model_nano: str = ""
    anthropic_model_mini: str = ""
    anthropic_model_reason: str = ""
    anthropic_model_embed: str = ""

    # ---- Google (Gemini) -------------------------------------------------------
    #
    # One fixed endpoint, same shape as the Anthropic block above: a shared
    # key/model with per-tier overrides, translated to and from the OpenAI wire
    # shape by ModelRouter._google_client() / the adapter classes next to it.
    google_api_key: str = ""
    google_api_key_nano: str = ""
    google_api_key_mini: str = ""
    google_api_key_reason: str = ""
    google_api_key_embed: str = ""
    # Real model names - e.g. "gemini-3.5-flash". No default, same reasoning as
    # openai_model_*/anthropic_model_*: an unconfigured tier cannot be resolved
    # and falls back to mock rather than guessing at a model name.
    google_model_nano: str = ""
    google_model_mini: str = ""
    google_model_reason: str = ""
    google_model_embed: str = ""

    # ---- what a vision step costs -------------------------------------------
    #
    # Three settings, one problem: a desktop agent that looks at its screen on
    # every step is priced in *images*, and an image is roughly a thousand
    # prompt tokens whatever the model. The defaults here were chosen against
    # the sidecar's own numbers (see `infra/bot-desktop/sidecar/server.py`): a
    # full-screen PNG is ~1.5 MB of base64 (the sidecar's own figure for a real
    # capture) and a 1280x800 one prices at 1105 prompt tokens; the
    # same frame as JPEG q70 capped at 1024px wide is 765 prompt tokens and a
    # small fraction of the bytes, and a UI is still perfectly legible in it.
    #
    # Note which half of that is which. The token saving is only 1.44x, because
    # the image-token formula already refits everything into a 768px short edge
    # before it counts 512px tiles — the cliff is at a 512px *short* edge
    # (`max_width=819` on a 1280x800 screen: 425 tokens), not at 1024px wide.
    # The bytes are where the latency lives. Screenshot pruning, not this, is
    # what moved the bill.
    #
    # These apply to the *agent's* screenshots only. `GET
    # /bots/{id}/desktop/screenshot` still serves a full-size PNG, because
    # docs/API.md pins `png_base64` and a human looking at their bot's screen
    # wants the real pixels.
    #
    # `agent_screenshot_max_width` changes the coordinate space the model
    # clicks in. That is handled by `desktop.ScreenGeometry`, which maps every
    # point back onto the true desktop before it reaches the sidecar. Do not
    # set this without keeping that mapping in mind: a silent rescale is a
    # click that lands somewhere else.
    agent_screenshot_format: str = "jpeg"  # jpeg | png
    agent_screenshot_quality: int = 70
    agent_screenshot_max_width: int = 1024
    agent_screenshot_grayscale: bool = False

    #: How many screenshots stay in the live conversation as *images*. Older
    #: ones are replaced by a one-line placeholder before every model call.
    #:
    #: This is the single biggest cost lever in the product. Without it the
    #: conversation keeps every frame it ever took, so a 35-step run re-sends
    #: 1+2+...+35 = 630 images and the bill grows with the *square* of the step
    #: count. Two is the current screen plus the one before it, which is what a
    #: model needs to tell whether its last action did anything; the loop's own
    #: byte-identical-screen detector covers the rest.
    agent_screenshot_history: int = 2

    # ---- what a DOM step costs ----------------------------------------------
    #
    # The browser lane's win is reliability, not size, and it is worth being
    # precise about that because the obvious assumption is wrong. Measured on
    # the sidecar image: a full 200-element snapshot of a Wikipedia article is
    # ~3 000 text tokens, while the same page as a 1024px JPEG is ~1 300 vision
    # tokens - so a careless snapshot is more than twice the price of the
    # screenshot it replaces. What it buys is that every line carries a `ref`
    # the model can act on, and a `ref` cannot be off by fourteen pixels the way
    # a guessed coordinate can.
    #
    # These three keep the default snapshot economical. Same article: 12 672 B
    # unfiltered, 4 169 B with `viewport_only`, 2 965 B at `max_elements=60`.
    # The model can override every one of them per call, and the rendered result
    # always says how many elements it is not being shown, so a truncated page
    # is never silently a short one.
    agent_browser_snapshot_max_elements: int = 100
    agent_browser_snapshot_viewport_only: bool = True
    agent_browser_snapshot_max_text: int = 20

    # ---- how hard the model thinks about each kind of call -------------------
    #
    # An empty string means "send no `reasoning_effort`", which leaves the
    # deployment to reason as it sees fit. The values below are chosen against
    # what the live deployments actually accept next to function tools, which is
    # narrower than the API reference suggests — the probe results and the
    # measured latencies are recorded on `REASONING_EFFORTS` in
    # `services/model_router.py`, and the short version is:
    #
    #   gpt-5.6-sol / gpt-5.6-terra   only "" or "none" are legal with tools
    #   gpt-5.4-mini                  the full graded scale is legal
    #
    # `agent_effort_step` is the one that matters. Ordinary desktop steps run
    # on the reason tier, where `"none"` took a measured 1.39s against 2.35s
    # for the default — and the model still emitted the right tool call every
    # time. `agent_effort_opening` is the `agent_turn` call on the mini tier,
    # which decides whether to act at all and is where this product's original
    # "I'm going to start by…" bug lived, so it is allowed to think a little.
    # `agent_effort_recover` is empty on purpose: after a failure, a frozen
    # screen or a refusal to act, the reason tier should think — and since it
    # cannot be asked for *more* than its default, the lever there is to stop
    # suppressing it.
    #
    # A value a deployment rejects costs one wasted request, once, and is then
    # remembered and dropped. It is not a way to break the loop.
    agent_effort_step: str = "none"
    agent_effort_opening: str = "low"
    agent_effort_recover: str = ""

    bot_desktop_mode: str = "docker"  # docker | mock | aci | k8s | aks
    bot_desktop_image: str = "nesqbot/bot-desktop:local"
    bot_desktop_network: str = "nesqbot_default"
    bot_desktop_home_root: str = "./data/bot-homes"
    bot_desktop_stream_base: str = "http://localhost:6901"
    # Empty -> docker.from_env(); set to e.g. unix:///var/run/docker.sock or a
    # tcp:// endpoint to talk to a specific daemon.
    bot_desktop_docker_host: str = ""

    # Azure Container Instances backing for per-bot desktops. One container
    # group per bot: hypervisor-isolated, its own filesystem and identity, and
    # billed per second so an idle roster costs nothing. Chosen over AKS because
    # desktops are bursty - AKS charges a node floor around the clock, and pods
    # share a node kernel, which is a weaker boundary than a container group.
    aci_resource_group: str = ""
    aci_subscription_id: str = ""
    aci_region: str = "swedencentral"
    aci_subnet_id: str = ""  # delegated to Microsoft.ContainerInstance; no public IP
    aci_cpu: float = 2.0
    aci_memory_gb: float = 4.0
    aci_registry_server: str = ""
    aci_registry_identity: str = ""  # user-assigned identity with AcrPull
    aci_start_timeout_seconds: int = 180  # cold pull of the desktop image

    # Generic self-hosted Kubernetes backing for per-bot desktops (bot_desktop_mode
    # = "k8s"). One Pod per bot against whatever cluster the kubeconfig points at -
    # k3s, kind, EKS, GKE, bare metal, anything. Unlike `aci`, which is Azure-only
    # and always destructive on stop, k8s desktops get real persistence: a
    # PersistentVolumeClaim when k8s_storage_class is set, or a hostPath directory
    # (single-node/dev only) when it is not.
    k8s_namespace: str = "nesqbot"
    # Empty -> in-cluster config when the API itself runs in the cluster, else the
    # default kubeconfig (~/.kube/config). Set to point at a specific kubeconfig
    # file when the API runs outside the cluster it manages desktops in.
    k8s_kubeconfig_path: str = ""
    k8s_context: str = ""  # empty -> kubeconfig's current-context
    # Empty -> hostPath fallback at k8s_host_path_root, documented dev/single-node
    # only: the volume is pinned to whichever node the pod lands on, so a
    # multi-node cluster needs a real StorageClass here instead.
    k8s_storage_class: str = ""
    k8s_host_path_root: str = "/var/lib/nesqbot/bot-homes"
    k8s_pvc_size_gi: int = 5
    k8s_cpu_request: str = "250m"
    k8s_cpu_limit: str = "2"
    k8s_memory_request: str = "512Mi"
    k8s_memory_limit: str = "4Gi"
    k8s_image_pull_secret: str = ""  # name of an existing imagePullSecret, if any
    k8s_service_type: str = "ClusterIP"  # ClusterIP | NodePort
    # Required when k8s_service_type is NodePort: the host/IP a client outside the
    # cluster reaches a node at. A ClusterIP desktop is only reachable from inside
    # the cluster (or via the self-hoster's own ingress), which is why this is a
    # hard requirement rather than a fallback - an unset public host on NodePort
    # would otherwise silently hand back an unreachable URL.
    k8s_public_host: str = ""
    k8s_start_timeout_seconds: int = 180  # cold pull of the desktop image

    # Shared secret for the bot-desktop sidecar. When empty the sidecar runs
    # open (local dev) and we send no auth header at all.
    nesq_sidecar_token: str = ""

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "nesq-bot"
    temporal_connect_timeout_seconds: float = 3.0
    temporal_rpc_timeout_seconds: float = 10.0

    default_bot_daily_budget_usd: float = 5.0
    azure_key_vault_url: str = ""
    bots_dir: str = "../../bots"

    # Connector vendor calls. `connector_live_calls=false` is the kill switch:
    # every connector action falls back to its mock, whatever else is set.
    # A connector still needs somewhere real to call — `graph_api_base_url` for
    # Microsoft Graph, a `base_url` in the manifest for everything else — so an
    # empty base URL means that connector mocks.
    connector_live_calls: bool = True
    graph_api_base_url: str = ""

    # Outbound call budgets (seconds)
    request_timeout_seconds: float = 60.0

    #: How often the API re-runs the orphaned-run reaper while it is
    #: alive. Reaping used to happen only at boot, so a run that stalled
    #: after the last deploy stayed `running` for ever and the person
    #: watching that thread was told nothing - see `main._sweep_orphaned_runs`.
    #: Ten minutes against `reaper.STALE_AFTER` (45m) bounds the delay
    #: between a turn dying and the thread saying so at ~55 minutes.
    run_sweep_interval_seconds: int = 600
    #: How often the API looks for work items whose owner has not been woken —
    #: see `services.work_dispatch` and `main._dispatch_assigned_work`.
    #:
    #: Short, unlike the reaper above, because this one is in the person's way:
    #: a chief of staff that assigns three items and says so has made a promise
    #: that those bots are starting, and a minute of apparent silence reads as
    #: the same nothing-happened this whole lane exists to end. Fifteen seconds
    #: costs one cheap indexed query per replica per interval and nothing else —
    #: the query is a partial index lookup over the backlog, not a table scan.
    work_dispatch_interval_seconds: int = 15
    sidecar_timeout_seconds: float = 30.0
    redis_connect_timeout_seconds: float = 2.0

    # Retrieval
    embedding_dim: int = 1536
    rag_max_candidates: int = 200

    # Entra ID token validation
    entra_jwks_cache_seconds: int = 3600
    entra_allowed_algorithms: str = "RS256"
    # Tolerance for clock drift between this host and Entra when checking `exp`
    # and `nbf`. Small on purpose: it is a skew allowance, not a grace period.
    entra_clock_skew_seconds: int = 60

    # Mobile push (Expo) — approval notifications
    expo_push_url: str = "https://exp.host/--/api/v2/push/send"
    expo_push_enabled: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def entra_algorithm_list(self) -> list[str]:
        return [a.strip() for a in self.entra_allowed_algorithms.split(",") if a.strip()]

    @property
    def is_development(self) -> bool:
        return self.nesq_env.strip().lower() == "development"


def configure_logging(settings: Settings) -> None:
    """Give the application's own loggers somewhere to write.

    uvicorn's default dictConfig configures only the `uvicorn*` loggers and
    leaves the root logger with no handler at all. Every
    `logging.getLogger("app.…")` record below WARNING is therefore discarded in
    the container, and WARNING and above escape only through
    `logging.lastResort`. That is why the deployed API could not answer "which
    Azure auth mode did you pick?" — `ModelRouter` was logging it, at INFO, into
    a logger with nowhere to go.

    Idempotent, and deliberately passive: if anything has already configured the
    root logger — pytest's capture plugin, a `--log-config`, an embedding host —
    this leaves it alone.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=(settings.log_level or "INFO").strip().upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # An INFO root turns these into a request-by-request firehose — azure.core
    # logs every IMDS and Foundry round trip, headers included. Our own records
    # are the point; theirs are noise until something is wrong.
    for noisy in ("azure", "azure.core", "azure.identity", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    configure_logging(settings)
    return settings
