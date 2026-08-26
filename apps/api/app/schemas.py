from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Mirrors `Provider` in `app/services/model_router.py`. Hand-synced rather
#: than imported so this leaf module stays dependency-free of `app.services`;
#: a value outside this set is caught here as a 422 rather than surfacing
#: later as "this bot silently mocks", which `ModelRouter._provider_for`
#: would otherwise degrade to.
KNOWN_MODEL_PROVIDERS = frozenset({"azure", "openai", "anthropic", "google"})


def _validate_model_override(model: Any) -> Any:
    """Shared by `CreateCustomBotIn` and `UpdateBotIn`.

    `model_provider` set with no `model_name` resolves to an empty model name
    at call time (`ModelRouter.model_name`) and the bot silently falls back to
    mock — correct behaviour for the router, a confusing one for whoever just
    configured this bot in the setup wizard and got no error. Caught here
    instead, as a 422 naming exactly what is missing.
    """
    if model.model_provider is not None and model.model_provider not in KNOWN_MODEL_PROVIDERS:
        raise ValueError(
            f"model_provider must be one of {sorted(KNOWN_MODEL_PROVIDERS)}, got {model.model_provider!r}"
        )
    if model.model_provider is not None and not (model.model_name or "").strip():
        raise ValueError("model_name is required when model_provider is set")
    if model.model_name is not None and model.model_provider is None:
        raise ValueError("model_provider is required when model_name is set")
    return model


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str

    class Config:
        from_attributes = True


class BotOut(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str
    is_system: bool
    daily_budget_usd: float
    desktop_profile: str
    #: NULL means "the router's tier routing decides" — the historical and
    #: still-default behaviour. See Bot.model_provider in models.py.
    model_provider: str | None = None
    model_name: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class CreateCustomBotIn(BaseModel):
    name: str
    role: str
    system_prompt: str
    connector_ids: list[str] = []
    mcp_ids: list[UUID] = []
    desktop_profile: str = "xfce"
    daily_budget_usd: float = 5.0
    model_provider: str | None = None
    model_name: str | None = None

    @model_validator(mode="after")
    def _provider_and_model_travel_together(self) -> "CreateCustomBotIn":
        return _validate_model_override(self)


class ProvidersOut(BaseModel):
    """Which providers this deployment can actually reach right now.

    A live credential resolved, not just a name this build recognises — see
    `ModelRouter.provider_available`. For the setup wizard: which providers
    can be offered per bot before a self-hoster hits "this bot mocks" because
    nobody set `ANTHROPIC_API_KEY`.
    """

    azure: bool
    openai: bool
    anthropic: bool
    google: bool


class ThreadOut(BaseModel):
    id: UUID
    title: str
    bot_ids: list[UUID]
    created_at: datetime
    updated_at: datetime


class CreateThreadIn(BaseModel):
    bot_ids: list[UUID]
    title: str | None = None
    initial_message: str | None = None


class MessageOut(BaseModel):
    id: UUID
    thread_id: UUID
    bot_id: UUID | None
    user_id: UUID | None
    role: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class SendMessageIn(BaseModel):
    content: str
    mention_bot_ids: list[UUID] = []


class DesktopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bot_id: UUID
    state: str
    stream_url: str | None
    control_url: str | None
    container_id: str | None
    last_error: str | None


class DesktopStreamTicketOut(BaseModel):
    """Answer to `POST /bots/{bot_id}/desktop/stream/ticket`.

    A Bot Desktop has no public address by design, so the noVNC stream is served
    back through this API. Neither an `<iframe src>` nor a `WebSocket` handshake
    can carry `Authorization`, so the viewer authenticates with this ticket
    instead: short-lived, bound to the calling user and this bot, and burned by
    the WebSocket that redeems it.

    `stream_path` and `ws_path` are relative to the API root (the `/api` mount),
    not to the host - the API does not guess its own public origin, and clients
    already hold that in their base URL. `ws_path` is the *control* connection,
    and connecting it consumes the ticket: a second viewer presenting the same
    one is refused. The assets under `stream_path` keep resolving until the
    ticket expires, because noVNC is still fetching some of them after it
    connects and a half-painted page protects nothing.
    """

    ticket: str
    expires_at: datetime
    #: Whole seconds of validity left at the moment the response was built.
    expires_in: int
    stream_path: str
    ws_path: str
    #: What the desktop's VNC server expects, so the viewer does not have to
    #: stop and ask the human for a password they were never told. It is the
    #: same constant every driver bakes into the image (`VNC_PW`), and it is not
    #: the boundary - the private IP and this ticket are. A per-bot secret would
    #: need somewhere to store it and a way for the viewer to fetch it; when
    #: that exists, this field is where it arrives.
    vnc_password: str | None = None


class DesktopActionIn(BaseModel):
    """Body for `POST /bots/{bot_id}/desktop/action`.

    `risk` is **escalate-only**, matching `McpCallIn` and the routine step
    branches: the action name is classified server-side by
    `services.risk.classify_action_risk` and a declared risk can only raise the
    result, never lower it. Without the field a taught step's declared risk was
    honoured inline but silently dropped over HTTP - the worker has been sending
    it on desktop bodies with nothing to receive it.
    """

    action: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    button: str | None = None
    keys: list[str] = []
    risk: str | None = None
    # Wheel scrolling. `infra/bot-desktop/sidecar/server.py` has read
    # `body.direction` and `body.amount` since it was written, and this body had
    # nowhere to put them - so every scroll that ever reached the sidecar, from
    # the desktop pane, from a routine step and from the agent loop, silently
    # collapsed to the sidecar's defaults of "down, 3 clicks". Reading a results
    # page is mostly scrolling, and a loop that can only nudge three clicks at a
    # time spends its whole step budget getting nowhere.
    #
    # Both optional, so every existing caller is unaffected: `desktop_action`
    # dumps with `exclude_none=True`, so an unset field is not forwarded and the
    # sidecar's own default still applies. The bounds mirror `ActionIn` exactly,
    # which is what stops a model asking for ten thousand clicks.
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: int | None = Field(default=None, ge=1, le=50)


class ApprovalOut(BaseModel):
    id: UUID
    run_id: UUID | None = None
    bot_id: UUID
    risk: str
    title: str
    summary: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: UUID | None = None
    note: str | None = None
    execution: dict[str, Any] | None = None

    class Config:
        from_attributes = True


class ApprovalDecisionIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = None


class StandingApprovalOut(BaseModel):
    """One standing permission — *"don't ask again for this button"*.

    Everything a person needs to decide whether to keep it, and nothing they
    would have to be an engineer to read. `permits` and `place` are rendered
    server-side from the same vocabulary the reply and the approval card use, so
    the three surfaces cannot drift into three descriptions of one grant.

    The provenance fields are not decoration and are not optional: `origin`,
    `note`, `source_approval_ids` and `granted_at` are the answer to "who
    allowed this and on what evidence", which is the question an audit opens
    with. `used` and `last_used_at` are the answer to "and what did it do".
    """

    id: UUID
    bot_id: UUID
    action: str
    risk: str
    #: `button "Message"` — the accessible name Chrome computed, quoted.
    element: str
    #: scheme+host+path, as matched.
    url: str
    #: `linkedin.com/in/andrei-pop` — the page as a person reads it.
    place: str
    #: `click "Message" on linkedin.com/in/andrei-pop`.
    permits: str
    #: `note` | `repetition`.
    origin: str
    #: Verbatim, when they asked in writing. Empty otherwise.
    note: str
    source_approval_ids: list[str]
    used: int
    last_used_at: datetime | None = None
    granted_at: datetime
    revoked_at: datetime | None = None


class StandingApprovalListOut(BaseModel):
    """The list, plus the limit that is not negotiable.

    `always_asks` rides along with the collection rather than being hardcoded in
    a client, because the sentence is a promise about the gate and the gate is
    server-side. A UI that has to restate it is a UI that can restate it wrong.
    """

    items: list[StandingApprovalOut]
    always_asks: str


class ConnectorOut(BaseModel):
    id: str
    name: str
    version: str
    auth: str
    scopes: list[Any]
    actions: list[Any]
    risk_default: str
    first_party: bool

    class Config:
        from_attributes = True


class RegisterConnectorIn(BaseModel):
    id: str
    name: str
    version: str = "1.0.0"
    auth: str = "api_key"
    scopes: list[str] = []
    actions: list[dict[str, Any]] = []
    risk_default: str = "observe"
    first_party: bool = False


class BindConnectorIn(BaseModel):
    secret_ref: str | None = None
    status: str = "connected"


class RegisterMcpIn(BaseModel):
    name: str
    transport: str
    endpoint: str | None = None
    command: str | None = None
    tool_allowlist: list[str] = []


class McpOut(BaseModel):
    id: UUID
    name: str
    transport: str
    endpoint: str | None
    command: str | None
    enabled: bool
    tool_allowlist: list[Any]

    class Config:
        from_attributes = True


class RoutineIn(BaseModel):
    bot_id: UUID
    name: str
    description: str = ""
    steps: list[dict[str, Any]]
    schedule_cron: str | None = None


class RoutineOut(BaseModel):
    id: UUID
    bot_id: UUID
    owner_user_id: UUID | None = None
    name: str
    description: str
    steps: list[Any]
    schedule_cron: str | None
    version: int
    enabled: bool

    class Config:
        from_attributes = True


class TeachRoutineIn(BaseModel):
    bot_id: UUID
    name: str
    description: str = ""
    recorded_steps: list[dict[str, Any]]
    schedule_cron: str | None = None


class UsageOut(BaseModel):
    bot_id: UUID
    bot_name: str
    spent_usd_today: float
    budget_usd: float
    entries: list[dict[str, Any]]


class EvalCaseIn(BaseModel):
    name: str
    prompt: str
    expect_contains: list[str] = []


class TokenOut(BaseModel):
    access_token: str
    user: UserOut


# ---------------------------------------------------------------------------
# v0.3 surface (docs/API.md)
# ---------------------------------------------------------------------------


class UpdateBotIn(BaseModel):
    """Partial bot update. Every field is optional; unset fields are untouched."""

    name: str | None = None
    role: str | None = None
    slug: str | None = None
    system_prompt: str | None = None
    daily_budget_usd: float | None = Field(default=None, ge=0)
    desktop_profile: str | None = None
    #: Unlike every other field here, `null` is meaningful and distinct from
    #: "not sent": sending `model_provider: null` clears the override and
    #: reverts this bot to tier routing. See update_bot's handling of these
    #: two fields specifically.
    model_provider: str | None = None
    model_name: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _known_model_provider(cls, data: Any) -> Any:
        """Enum check only. Unlike `CreateCustomBotIn`, this cannot also
        enforce "both or neither": a PATCH sending only `model_name` to swap
        the model under an already-configured provider (or only
        `model_provider` to clear an already-consistent one) is legitimate
        and this validator has no DB row to check the other field against.
        `update_bot` enforces the pairing against the *resulting* row instead,
        after the change is applied — see there.
        """
        if not isinstance(data, dict):
            return data
        provider = data.get("model_provider")
        if "model_provider" in data and provider is not None and provider not in KNOWN_MODEL_PROVIDERS:
            raise ValueError(
                f"model_provider must be one of {sorted(KNOWN_MODEL_PROVIDERS)}, got {provider!r}"
            )
        return data


class BudgetIn(BaseModel):
    daily_budget_usd: float = Field(ge=0)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    thread_id: UUID | None = None
    bot_id: UUID
    routine_id: UUID | None = None
    status: str
    temporal_workflow_id: str | None = None
    context_ledger: dict[str, Any] = Field(default_factory=dict)
    detail: dict[str, Any] | None = None
    error: str | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_user_id: UUID | None = None
    bot_id: UUID | None = None
    event_type: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ExecuteActionIn(BaseModel):
    """Body for `POST /bots/{bot_id}/connectors/{connector_id}/actions/{action}`."""

    input: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    thread_id: UUID | None = None


class PendingApprovalOut(BaseModel):
    """Returned (201) when an action is risk-gated instead of executed."""

    approval_id: UUID | None = None
    status: str = "pending_approval"
    risk: str
    title: str
    detail: str | None = None


class BotConnectorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bot_id: UUID
    connector_id: str
    name: str
    status: str
    secret_ref: str | None = None
    risk_default: str = "observe"
    first_party: bool = False
    actions: list[Any] = Field(default_factory=list)


class UpdateMcpIn(BaseModel):
    name: str | None = None
    transport: str | None = None
    endpoint: str | None = None
    command: str | None = None
    enabled: bool | None = None
    tool_allowlist: list[str] | None = None


class McpToolsOut(BaseModel):
    mcp_id: UUID
    name: str
    tools: list[dict[str, Any]] = Field(default_factory=list)
    mock: bool = False
    error: str | None = None


class McpCallIn(BaseModel):
    """Body for `POST /bots/{bot_id}/mcp/{mcp_id}/call`.

    `risk` is **escalate-only**: the tool name is classified server-side by
    `services.desktop.classify_action_risk` and a declared risk can only raise
    the result, never lower it.
    """

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk: str | None = None


class UpdateRoutineIn(BaseModel):
    name: str | None = None
    description: str | None = None
    steps: list[dict[str, Any]] | None = None
    schedule_cron: str | None = None
    enabled: bool | None = None


class RoutineRunOut(BaseModel):
    workflow_id: str | None = None
    run_id: str | None = None
    inline: bool = False
    status: str = "started"
    detail: str | None = None


class MemoryIn(BaseModel):
    kind: str = "note"
    content: str


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_id: UUID | None = None
    user_id: UUID | None = None
    kind: str
    content: str
    created_at: datetime


class KbArticleIn(BaseModel):
    title: str
    body: str


class KbArticleUpdateIn(BaseModel):
    title: str | None = None
    body: str | None = None


class KbArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    body: str
    created_at: datetime
    score: float | None = None


class EvalSuiteIn(BaseModel):
    cases: list[EvalCaseIn] = Field(default_factory=list)


class EvalSuiteOut(BaseModel):
    passed: int
    total: int
    results: list[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float = 0.0


class HealthOut(BaseModel):
    """`GET /health`.

    Modelled rather than returned as a bare dict so `check-api-parity` compares
    it against the TypeScript `Health` interface. It was a bare dict when the
    desktop footer drifted out of sync with the server payload; nothing caught
    it because nothing was comparing the two.
    """

    ok: bool
    service: str = "nesqbot-api"
    #: Hand-maintained contract number. See `API_VERSION`.
    version: str
    #: Image tag stamped at build time, or "unknown" when unstamped.
    build: str


class HealthDeepOut(BaseModel):
    ok: bool
    service: str = "nesqbot-api"
    version: str
    checks: dict[str, Any] = Field(default_factory=dict)


class EntraLoginIn(BaseModel):
    id_token: str


class DeviceRegisterIn(BaseModel):
    token: str = Field(min_length=1)
    platform: str = Field(pattern="^(ios|android|web)$")


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    token: str
    platform: str
    created_at: datetime | None = None


class ScreenshotOut(BaseModel):
    ok: bool
    width: int = 0
    height: int = 0
    png_base64: str = ""
    mock: bool = False
    error: str | None = None


class DesktopWindowsOut(BaseModel):
    ok: bool
    windows: list[dict[str, Any]] = Field(default_factory=list)
    mock: bool = False
    error: str | None = None


class OkOut(BaseModel):
    ok: bool = True
    detail: str | None = None


class RunStatusIn(BaseModel):
    """Worker callback body for `POST /runs/{run_id}/status`."""

    status: str
    error: str | None = None
    detail: dict[str, Any] | None = None
    routine_id: UUID | None = None
    thread_id: UUID | None = None
    bot_id: UUID | None = None
    workflow_id: str | None = None


class ResumeRunIn(BaseModel):
    """Body for `POST /runs/{run_id}/resume` — the "I've finished, continue" button.

    Everything the resumed run needs is already on the run itself; `note` is the
    one thing only the person at the screen knows, and it is optional.
    """

    note: str = Field(default="", max_length=2000)


class ResumeRunOut(BaseModel):
    """What the resume button gets back.

    `resumed` is the idempotency answer. A second click on a run that is no
    longer `awaiting_human` is not an error — it is a double-click — so it comes
    back `ok=true, resumed=false` with the status the run actually has, and no
    second loop is started.
    """

    ok: bool = True
    resumed: bool
    run_id: UUID
    status: str
    detail: str | None = None
    thread_id: UUID | None = None
    bot_id: UUID | None = None
    message_id: UUID | None = None
    message: str | None = None
    outcome: str | None = None
    approval_id: UUID | None = None
    cost_usd: float | None = None


class CreateApprovalIn(BaseModel):
    """Direct approval creation, used by routine steps of `type: "approval"`."""

    bot_id: UUID
    run_id: UUID | None = None
    risk: str = "send"
    title: str
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rehearsal and reversibility (docs/API.md — dry runs, plans, the undo log)
# ---------------------------------------------------------------------------


class PlannedCallOut(BaseModel):
    """One effect as it *would* have happened — `simulation.PlannedCall`.

    `input` (not `input_data`) matches the wire shape `PlannedCall.as_dict()`
    produces, which is also what the content hash is taken over.
    """

    step_index: int
    kind: str
    connector_id: str | None = None
    mcp_id: str | None = None
    action: str
    input: dict[str, Any] = Field(default_factory=dict)
    risk: str
    requires_approval: bool
    summary: str
    ok: bool = True
    problems: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    binding: dict[str, Any] | None = None
    reversible: bool = False
    undo_note: str = ""


class PlanVerdictOut(BaseModel):
    """The summary a reviewer reads before expanding anything."""

    steps_total: int = 0
    would_gate: list[int] = Field(default_factory=list)
    would_fail: list[dict[str, Any]] = Field(default_factory=list)
    would_execute: int = 0
    reversible: list[int] = Field(default_factory=list)
    ok: bool = True


class PlanOut(BaseModel):
    """A produced plan. Returned by every dry run; never persisted by itself."""

    bot_id: UUID
    routine_id: UUID | None = None
    name: str = ""
    created_at: datetime
    content_hash: str
    verdict: PlanVerdictOut
    calls: list[PlannedCallOut] = Field(default_factory=list)


class PlanRecordOut(BaseModel):
    """A saved plan. `content_hash` is what execution re-derives and compares."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_id: UUID
    routine_id: UUID | None = None
    created_by: UUID | None = None
    name: str = ""
    status: str = "draft"
    content_hash: str
    steps: list[Any] = Field(default_factory=list)
    plan: PlanOut | None = None
    steps_total: int = 0
    gated_steps: int = 0
    failing_steps: int = 0
    executed_run_id: UUID | None = None
    created_at: datetime
    executed_at: datetime | None = None


class DryRunActionIn(BaseModel):
    """Body for the connector-action dry run. Mirrors `ExecuteActionIn.input`."""

    input: dict[str, Any] = Field(default_factory=dict)


class SavePlanIn(BaseModel):
    """Persist a plan so a human can approve *the plan* and then execute it.

    The plan is never taken from the client: the server re-runs the rehearsal
    from `routine_id`, or from `bot_id` + `steps`, and saves what it produced.
    A client that already showed a plan to a human can pass
    `expected_content_hash`; a mismatch is refused as `plan_drifted` rather than
    silently saving different work under the same review.
    """

    routine_id: UUID | None = None
    bot_id: UUID | None = None
    steps: list[dict[str, Any]] | None = None
    name: str = ""
    status: str = Field(default="draft", pattern="^(draft|approved)$")
    expected_content_hash: str | None = None


class ActionLogOut(BaseModel):
    """One executed effect and its inverse, where one exists."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_id: UUID
    run_id: UUID | None = None
    approval_id: UUID | None = None
    actor_user_id: UUID | None = None
    kind: str
    connector_id: str | None = None
    mcp_id: UUID | None = None
    action: str
    risk: str = "observe"
    target_ref: str | None = None
    input_data: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    reversible: bool = False
    irreversible_reason: str | None = None
    compensator: dict[str, Any] = Field(default_factory=dict)
    undone: bool = False
    undone_at: datetime | None = None
    undone_by: UUID | None = None
    undo_result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class UndoResultOut(BaseModel):
    """The outcome of running one compensator."""

    ok: bool
    action_log_id: UUID
    kind: str
    action: str
    compensator: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ReversibilityRowOut(BaseModel):
    """One row of the reversibility matrix — what can be taken back, and what cannot."""

    connector_id: str | None = None
    action: str
    reversible: bool
    compensator: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Work items — owned, transferable work and the ledger of who held it
# ---------------------------------------------------------------------------

#: The four states a work item can be in.
#:
#: Kept deliberately narrow. `open` is "nobody has touched it", `working` is
#: "the owning bot is acting", `waiting` is "we are blocked on the outside
#: world" — the state a lead sits in between an outreach going out and a reply
#: coming back — and `closed` is terminal. Anything finer belongs in
#: `resolution` or in `detail`, not in a state machine every caller has to
#: reason about. The service tuple `services.work_items.WORK_ITEM_STATUSES`
#: mirrors this; a test asserts the two cannot drift.
WorkItemStatus = Literal["open", "working", "waiting", "closed"]


class WorkItemKeyIn(BaseModel):
    """An external identity to recognise this work item by on the way back in.

    `channel` names the space the value lives in (`email`, `phone`, `linkedin`,
    `crm`, `ticket`); `value` is the identifier itself. Both are normalised
    server-side — trimmed, lowercased — so a webhook that shouts an address in
    capitals still resolves.
    """

    channel: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=512)


class WorkItemKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    channel: str
    value: str


class WorkItemIn(BaseModel):
    """Body for `POST /work-items`.

    `reason` is not the work item's description — `summary` is. It is why *this
    bot* is the one holding it, and it lands on the opening row of the transfer
    ledger so "who has held this" has no gap at the front.
    """

    owner_bot_id: UUID
    title: str = Field(min_length=1, max_length=500)
    type: str = Field(default="lead", min_length=1, max_length=64)
    summary: str = Field(default="", max_length=8000)
    status: WorkItemStatus = "open"
    thread_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    keys: list[WorkItemKeyIn] = Field(default_factory=list)
    reason: str = Field(default="", max_length=2000)


class UpdateWorkItemIn(BaseModel):
    """Partial update. Unset fields are untouched.

    Ownership is not one of the fields, because ownership moves through
    `POST /work-items/{id}/transfer` and nowhere else — that is the only path
    that writes the ledger row, and a PATCH that could re-home an item would be
    a hole straight through the audit trail this entity exists for.

    Omitting the field is not enough to say that, though. Every other input
    model here inherits pydantic's default of *ignoring* unknown keys, so
    `PATCH {"owner_bot_id": "…"}` would parse, return 200 and a `WorkItemOut`
    still naming the old bot, and the caller would reasonably read that as a
    successful handover that produced no ledger row. A silent no-op on a write
    the caller believes succeeded is worse than the hole it was avoiding, and
    `owner_bot_id` is precisely the key a client would try.

    So this one model refuses extras. The inconsistency with the rest of the
    file is the point: everywhere else an unknown key is a client that is ahead
    of or behind the server, and dropping it is the tolerant thing to do. Here
    one specific unknown key means "I think I just moved ownership", and the
    only honest answer is 422 saying where ownership actually moves. The
    validator below fires first so that answer names `/transfer` instead of
    reading "Extra inputs are not permitted".
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _ownership_moves_through_transfer(cls, data: Any) -> Any:
        # Keyed on *presence*, not on truthiness: an explicit `null` is still a
        # caller asserting something about ownership, and answering it with 200
        # would be the same silent no-op by a shorter route.
        if isinstance(data, dict) and "owner_bot_id" in data:
            raise ValueError(
                "owner_bot_id cannot be changed here; transfer ownership with "
                "POST /work-items/{work_item_id}/transfer, which records who "
                "handed it over, to whom, and why"
            )
        return data

    title: str | None = Field(default=None, min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=8000)
    status: WorkItemStatus | None = None
    resolution: str | None = Field(default=None, max_length=500)
    thread_id: UUID | None = None
    detail: dict[str, Any] | None = None
    keys: list[WorkItemKeyIn] | None = None


class WorkItemOut(BaseModel):
    id: UUID
    type: str
    title: str
    summary: str
    status: str
    resolution: str | None = None
    #: Null only when the owning bot was deleted out from under the item.
    owner_bot_id: UUID | None = None
    owner_user_id: UUID
    thread_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    keys: list[WorkItemKeyOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    transferred_at: datetime | None = None
    #: Last time the *outside world* touched this — the inbound-events seam.
    #: Distinct from `updated_at`, which any edit moves.
    last_event_at: datetime | None = None
    closed_at: datetime | None = None


class WorkItemTransferIn(BaseModel):
    """Body for `POST /work-items/{work_item_id}/transfer`.

    `reason` is required, and `min_length=1` is the enforcement. A ledger of
    timestamps with no reasons is what the competitor does not have either; the
    sentence explaining the handover is the artefact a compliance reviewer reads.
    """

    to_bot_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    #: Set when a *bot* initiated the handover rather than the person at the
    #: keyboard. Must be a bot the caller can see; recorded alongside the human.
    actor_bot_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class WorkItemTransferOut(BaseModel):
    """One row of the handover ledger."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    work_item_id: UUID
    owner_user_id: UUID
    #: Null on the opening row written at creation — there was no predecessor.
    from_bot_id: UUID | None = None
    to_bot_id: UUID
    actor_user_id: UUID | None = None
    actor_bot_id: UUID | None = None
    reason: str = ""
    source: str = "api"
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkItemTransferResultOut(BaseModel):
    """What a transfer request gets back.

    `transferred` is the idempotency answer, and it is the same shape and the
    same reasoning as `ResumeRunOut.resumed`. Handing an item to the bot that
    already holds it is not an error — it is a retry, or a model calling the
    same tool twice — so it comes back `ok=true, transferred=false` with the
    item unchanged and, crucially, **no second ledger row** claiming a handover
    that did not happen.
    """

    ok: bool = True
    transferred: bool
    work_item: WorkItemOut
    transfer: WorkItemTransferOut | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Inbound events — the way the outside world reaches a bot
# ---------------------------------------------------------------------------

#: How a source is fed. `webhook` is pushed to over an unauthenticated,
#: HMAC-signed URL; `poll` is pulled from a connector the owner already bound.
#: Both converge on `services.inbound.ingest`.
InboundSourceKind = Literal["webhook", "poll"]

#: What became of one delivery. Every authenticated delivery ends as exactly one
#: of these and none of them means "discarded":
#:
#: * `matched`    — one work item; its bot was woken.
#: * `ambiguous`  — several candidates; the first was taken and the rest recorded.
#: * `unmatched`  — no work item. **A queue a person works**, not an error.
#: * `unroutable` — an item, but no bot or no human to answer for it.
#: * `duplicate`  — a replay of a delivery already on the record. Never a row.
InboundEventStatus = Literal["matched", "ambiguous", "unmatched", "unroutable", "duplicate"]


class InboundSourceIn(BaseModel):
    """Body for `POST /inbound/sources`.

    `slug` is absent on purpose and cannot be supplied. It is the public path
    segment of the hook URL, generated server-side from a CSPRNG: the column is
    globally unique like `bots.slug`, so a caller-chosen value would let one
    tenant take a name another wanted, and a guessable one would make the hook
    surface enumerable.

    `secret_ref` is a **reference** — `env://NAME`, `kv://vault/name`, or a bare
    name against the configured default vault — and is validated as one. A field
    that could hold a plaintext key would eventually hold one, and this row is
    served back by an owner-scoped API.
    """

    name: str = Field(default="", max_length=200)
    kind: InboundSourceKind = "webhook"
    #: The `work_item_keys.channel` a delivery resolves against when it does not
    #: name its own: `email`, `phone`, `linkedin`, `crm`.
    channel: str = Field(default="email", min_length=1, max_length=64)
    #: The bot a poll runs as, and the fallback when a matched item has none.
    bot_id: UUID | None = None
    #: Bots to seat when this lane has to **create** a thread for a reply.
    #:
    #: This is how "the lead answered, pass it to Sales" is possible at all.
    #: Thread membership is the delegation boundary — a bot cannot hand work to a
    #: bot that is not in the room — so the roster has to be named by a human,
    #: ahead of time, through this authenticated field. Nothing a model says can
    #: add to it, and every entry is re-checked for visibility when it is seated.
    bot_ids: list[UUID] = Field(default_factory=list)
    secret_ref: str | None = Field(default=None, max_length=500)
    connector_id: str | None = Field(default=None, max_length=100)
    #: For a poll source: `{"action": "list_inbox", "input": {}, "fields": {...}}`.
    #: `fields` remaps a connector's record keys onto `address`/`subject`/`body`/
    #: `external_id`; the defaults already match `microsoft_graph.list_inbox`.
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class UpdateInboundSourceIn(BaseModel):
    """Partial update. Unset fields are untouched.

    `slug` is not a field here either — rotating the URL means creating a new
    source, because a slug that can be changed in place is one that can be
    changed to another tenant's.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    channel: str | None = Field(default=None, min_length=1, max_length=64)
    bot_id: UUID | None = None
    bot_ids: list[UUID] | None = None
    secret_ref: str | None = Field(default=None, max_length=500)
    connector_id: str | None = Field(default=None, max_length=100)
    config: dict[str, Any] | None = None
    #: The kill switch. A disabled source refuses every delivery with the same
    #: answer an unknown slug gets, so turning one off tells an attacker nothing.
    enabled: bool | None = None


class InboundSourceOut(BaseModel):
    id: UUID
    slug: str
    #: `POST` here to deliver. Rendered so the owner can paste it into a provider
    #: without reassembling it from the slug and guessing the prefix.
    hook_path: str
    name: str
    kind: str
    channel: str
    owner_user_id: UUID
    bot_id: UUID | None = None
    bot_ids: list[UUID] = Field(default_factory=list)
    #: The reference, never the key. `services.secrets` resolves it in-process
    #: and the resolved value never reaches a row, a log line or a response.
    secret_ref: str | None = None
    connector_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    last_event_at: datetime | None = None
    last_polled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class InboundEventOut(BaseModel):
    """One thing that arrived, matched or not.

    `body` and `subject` are what the sender actually sent, stored untouched, so
    the record is of what was received rather than of what was kept. **Render
    them as text.** They are the only attacker-controlled strings in this API,
    and the sanitising this system does is on the way to the model
    (`services.inbound.render_untrusted`), not on the way to a client.
    """

    id: UUID
    source_id: UUID | None = None
    owner_user_id: UUID
    channel: str
    #: The normalised key value this resolved against, not necessarily a real
    #: address — a `From` header is forgeable.
    address: str
    external_id: str
    via: str
    status: str
    subject: str
    body: str
    work_item_id: UUID | None = None
    #: Every candidate, in `resolve_by_key` order, when there was more than one.
    #: `work_item_id` is always the first of them; the rest are here so a wrong
    #: guess on an ambiguous address is visible rather than silent.
    candidate_ids: list[UUID] = Field(default_factory=list)
    thread_id: UUID | None = None
    run_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    #: Null until the owning bot has actually been woken.
    handled_at: datetime | None = None


class InboundAckOut(BaseModel):
    """The **constant** answer to every authenticated delivery.

    Matched one work item, matched five, matched none, or was a replay of one
    already on the record: the body is byte-identical. That is the anti-
    enumeration property, and it is the whole reason this model has two fields
    and no ids. A sender who could tell "accepted and matched" from "accepted and
    matched nothing" could probe an unauthenticated endpoint for which addresses
    this tenant is working, one request at a time.

    The owner sees the real outcome at `GET /inbound/events`, where they are
    authenticated.
    """

    ok: bool = True
    status: str = "accepted"


class InboundPollOut(BaseModel):
    """What one `POST /inbound/sources/{id}/poll` fetched and what became of it.

    Informative, unlike `InboundAckOut`, because this endpoint is authenticated
    and owner-scoped: the caller is the person whose data it is.
    """

    ok: bool = True
    source_id: UUID
    fetched: int = 0
    #: Counts by `InboundEventStatus`, so "nothing matched" is one number.
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    unroutable: int = 0
    duplicates: int = 0
    event_ids: list[UUID] = Field(default_factory=list)
    detail: str | None = None


class CancelRunIn(BaseModel):
    """Why the run is being abandoned. Optional, but it lands in the audit."""

    reason: str | None = Field(default=None, max_length=500)


class CancelRunOut(BaseModel):
    """The escape hatch's answer.

    `cancelled` is the idempotency answer, the same shape as `ResumeRunOut.resumed`:
    cancelling a run that has already finished is not an error, it is a second
    press, and it comes back `ok=true, cancelled=false` with the status the run
    actually has.
    """

    ok: bool = True
    cancelled: bool
    run_id: UUID
    status: str
    detail: str | None = None
