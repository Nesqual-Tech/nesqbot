"""SQLAlchemy ORM models — mirrors sql/init.sql.

Every `created_at`/`updated_at` defaults to `clock_timestamp()`, never `now()`.
Postgres freezes `now()` at the *start of the transaction*, so a transaction
that writes several rows — a turn's audit events, a run's action log, a
message and its reply — stamps every one of them with the same instant, and
any `ORDER BY created_at` over them is then a sort with nothing to sort by.
`clock_timestamp()` is read per row, which is what these columns have always
claimed to record. `sql/init.sql` is the authority on the live schema and
carries the same defaults plus an idempotent upgrade for databases created
before this.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBEDDING_DIM = 1536


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    entra_oid: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class UserDevice(Base):
    __tablename__ = "user_devices"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    token: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    __table_args__ = (UniqueConstraint("user_id", "token", name="uq_user_devices_user_token"),)


class Bot(Base):
    __tablename__ = "bots"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("5.00"))
    desktop_profile: Mapped[str] = mapped_column(Text, default="xfce")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())


class ThreadBot(Base):
    __tablename__ = "thread_bots"
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("threads.id", ondelete="CASCADE"))
    bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id"), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: routine runs have no chat thread, and a run outlives the thread
    # it came from — runs are audit records, so thread deletion nulls the link.
    thread_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("threads.id", ondelete="SET NULL"), nullable=True)
    routine_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("routines.id", ondelete="SET NULL"), nullable=True, index=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id"))
    status: Mapped[str] = mapped_column(Text, default="queued")
    temporal_workflow_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_ledger: Mapped[dict] = mapped_column(JSONB, default=dict)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: routine steps park approvals outside any chat run. SET NULL, not
    # CASCADE — an approval is the record of what a human was asked to authorise
    # and must survive whatever produced it.
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id"))
    risk: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="pending")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution: Mapped[dict] = mapped_column(JSONB, default=dict)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BotDesktop(Base):
    __tablename__ = "bot_desktops"
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(Text, default="absent")
    container_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    control_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())


class Connector(Base):
    __tablename__ = "connectors"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    actions: Mapped[list] = mapped_column(JSONB, default=list)
    risk_default: Mapped[str] = mapped_column(Text, default="observe")
    first_party: Mapped[bool] = mapped_column(Boolean, default=False)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict)


class BotConnector(Base):
    __tablename__ = "bot_connectors"
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True)
    connector_id: Mapped[str] = mapped_column(Text, ForeignKey("connectors.id", ondelete="CASCADE"), primary_key=True)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="disconnected")


class McpServer(Base):
    __tablename__ = "mcp_servers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tool_allowlist: Mapped[list] = mapped_column(JSONB, default=list)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)


class BotMcp(Base):
    __tablename__ = "bot_mcp"
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True)
    mcp_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True)


class Routine(Base):
    __tablename__ = "routines"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"))
    # Nullable: pre-existing routines have no owner, and a genuinely unattended
    # routine is a valid state. Never backfilled with a stand-in human.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    schedule_cron: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class CostLedger(Base):
    __tablename__ = "cost_ledger"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"))
    tier: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="CASCADE"), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    # Deferred so ordinary SELECTs never pull the vector back; services/rag.py uses SQL.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class KbArticle(Base):
    __tablename__ = "kb_articles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class ContextLedger(Base):
    __tablename__ = "context_ledger"
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())


class PlanRecord(Base):
    """A produced dry-run plan, kept so a human can approve *the plan* itself.

    `content_hash` covers the intent of every planned call (index, kind, target,
    resolved input, risk, gate). Execution recomputes it and refuses to run when
    it no longer matches, so an approved plan cannot be swapped for a different
    one by editing the routine underneath it.

    Deliberately carries no foreign keys: like `audit_events`, a plan is a
    record of what was proposed and must outlive the routine that produced it —
    and an ad-hoc single-action plan has no routine row at all.
    """

    __tablename__ = "plan_records"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    routine_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="draft")
    content_hash: Mapped[str] = mapped_column(Text)
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    gated_steps: Mapped[int] = mapped_column(Integer, default=0)
    failing_steps: Mapped[int] = mapped_column(Integer, default=0)
    executed_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActionLog(Base):
    """One executed outbound effect, and its inverse where one exists.

    `reversible` is a promise the UI shows to a human, so it is only ever true
    when `compensator` holds a call that genuinely undoes the work. Everything
    else records `reversible=False` with `irreversible_reason` saying why.

    No foreign keys, for the same reason `audit_events` has none: this is the
    record that the work happened, and deleting the bot must not erase it.
    """

    __tablename__ = "action_log"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    connector_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text, default="observe")
    target_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    result_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    reversible: Mapped[bool] = mapped_column(Boolean, default=False)
    irreversible_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    compensator: Mapped[dict] = mapped_column(JSONB, default=dict)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undone_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    undo_result: Mapped[dict] = mapped_column(JSONB, default=dict)
    #: Which standing permission let this effect through, when no human was
    #: asked. NULL is the ordinary case — a human approved it, or its risk class
    #: never needed approving. Without this column the undo log can show a
    #: `send` that ran with nobody's approval behind it and offer nothing to
    #: explain why, which is the first question an audit asks.
    standing_approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class WorkItem(Base):
    """A unit of owned, transferable work — the thing one bot hands to another.

    Deliberately general with a `type`, not a `leads` table. A lead is the
    motivating case, not the mechanism: a support escalation, an invoice
    exception and an onboarding checklist all need the same three facts — an
    owning bot, the human it belongs to, and a recorded history of who was
    holding it when. Per-use-case tables would each need their own copy of the
    transfer ledger, and the ledger is the whole point: `docs/architecture.md`
    records that the competitor's audit view is "coming". One table means one
    queryable answer to "who handed this to whom, and why".

    `type` is free text rather than an enum for the same reason
    `connectors.risk_default` and `memories.kind` are: adding a work-item kind
    must not need a migration.

    Scoped to `owner_user_id`, which never changes. Only the *bot* is
    transferred. That keeps this out of the fallback chains `resolve_run_owner`
    and `resolve_approval_owner` need — those exist because a run or an approval
    can genuinely have no knowable human, whereas a work item is always created
    by or on behalf of one, and the one thing that writes to it unattended (an
    inbound reply) attaches to a row that already has an owner.
    """

    __tablename__ = "work_items"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(Text, default="lead")
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="open")
    # Only meaningful once `status` is terminal: the outcome a human reads off
    # the closed row ("won", "not a fit"). Kept separate from `status` so the
    # state machine stays four words wide and the vocabulary of outcomes can
    # grow without touching it.
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nullable, but required by `POST /work-items`. A custom bot can be deleted,
    # and taking the customer's pipeline with it would be worse than the row
    # existing unowned — "these leads have no bot" is a real state a human needs
    # to see and can fix with a transfer, so the FK is SET NULL rather than
    # CASCADE or RESTRICT.
    owner_bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Where the bots are talking about this item. Nullable and SET NULL: a work
    # item outlives the conversation that produced it, and an item created by a
    # routine has no thread at all. The inbound-events lane needs somewhere to
    # publish "the lead replied" — this is that somewhere.
    thread_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("threads.id", ondelete="SET NULL"), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())
    # Denormalised from the last `work_item_transfers` row so a list view can
    # sort by "recently handed over" without a correlated subquery per row.
    transferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last time something arrived from outside — the seam for the inbound lane.
    # Distinct from `updated_at`, which any PATCH moves: this one only moves when
    # the *outside world* acted, which is what "the lead answered" means.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkItemKey(Base):
    """An external identity a work item can be recognised by.

    This is the seam the inbound-events lane resolves against: a reply arrives
    carrying an email address, a phone number, a CRM record id or a LinkedIn
    profile, and the only question that matters is "which work item is this?".
    One indexed exact match on `(channel, value)` answers it.

    Deliberately **not** unique on `(channel, value)`. The same person can
    honestly be two work items — two sellers working the same account, or a lead
    that closed in March and came back in August — and a unique constraint would
    turn that into an IntegrityError raised at the *webhook*, discarding a real
    customer reply to protect a modelling assumption. `services.work_items`
    resolves ambiguity with a documented ordering instead, which fails visibly
    rather than silently.

    `owner_user_id` is copied down from the work item rather than joined. It is
    safe to denormalise precisely because it never changes — a transfer moves
    the *bot*, never the human — and it lets the inbound lane filter candidates
    by tenant with the single index lookup it already has to do.
    """

    __tablename__ = "work_item_keys"
    work_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("work_items.id", ondelete="CASCADE"), primary_key=True)
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class WorkItemTransfer(Base):
    """One recorded handover: who gave what to whom, when, and why.

    No foreign keys, exactly like `audit_events`, `plan_records` and
    `action_log`, and for the same reason: this is the record that the handover
    happened. Deleting the work item, or the bot that used to hold it, must not
    erase it. `owner_user_id` is stamped on the row so the ledger stays
    tenant-scopable after the work item it describes is gone.

    `from_bot_id` is NULL on exactly one row per item — the opening assignment
    written by `POST /work-items`. Recording creation as a transfer is what makes
    "read the transfers" a complete answer to "who has held this", instead of an
    answer with the first holder missing.

    `reason` is required by the API rather than defaulted. Without it the ledger
    is a list of timestamps, and the reason is the difference between this and a
    competitor whose audit view is still "coming".
    """

    __tablename__ = "work_item_transfers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    from_bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    to_bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    # Exactly one of these is normally set: a human reassigning from the UI, or
    # the bot that invoked the delegation tool. Both NULL means a system sweep.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    #: How the handover was triggered: "create", "api", or whatever the
    #: delegation lane stamps when `delegate_to_bot` drives one.
    source: Mapped[str] = mapped_column(Text, default="api")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class InboundSource(Base):
    """A configured way for the outside world to reach a bot — push or pull.

    Two kinds, one row shape, because the difference is only *who moves first*.
    A `webhook` source is delivered to; a `poll` source is fetched from. Both
    end up in `services.inbound.ingest`, and keeping them one table is what
    stops the two paths growing separate resolution rules — the failure mode
    where a reply that arrives by email is handled and the same reply pulled
    from a mailbox is not.

    `slug` is **server-generated and unguessable**, never chosen by the caller.
    It is the last path segment of the public hook URL, so a caller-chosen slug
    would be both a cross-tenant name grab (the column is globally unique, like
    `bots.slug`) and an enumerable surface. It is a capability, not a
    credential: the HMAC over `secret_ref` is what actually authenticates a
    delivery, and a source with no resolvable signing key refuses everything.

    `secret_ref` is a **reference** in `services.secrets` form (`env://NAME`,
    `kv://vault/name`), never the key. The row is returned by an owner-scoped
    API, and a column that could hold plaintext would eventually hold it.

    `bot_ids` is the roster this lane may seat when it has to *create* a thread
    for an inbound reply. It exists because thread membership is the delegation
    boundary (`orchestrator._delegate_targets`): a bot cannot hand work to a bot
    that is not in the room, so "the lead answered, pass it to Sales" needs
    Sales in the room, and the only acceptable way to put it there is a human
    naming it in configuration ahead of time. Nothing a model says can add to
    it, and it is re-checked for visibility at seat time.
    """

    __tablename__ = "inbound_sources"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, default="")
    #: `webhook` (delivered to) or `poll` (fetched from a connector).
    kind: Mapped[str] = mapped_column(Text, default="webhook")
    #: Default `work_item_keys.channel` for deliveries that do not name one.
    channel: Mapped[str] = mapped_column(Text, default="email")
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    #: The bot a poll runs as, and the fallback owner for an item with none.
    #: SET NULL for the same reason `work_items.owner_bot_id` is.
    bot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bots.id", ondelete="SET NULL"), nullable=True)
    bot_ids: Mapped[list] = mapped_column(JSONB, default=list)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    connector_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())


class InboundEvent(Base):
    """One thing that arrived from outside, matched or not.

    **This row is why an unmatched reply is not dropped on the floor.** A
    webhook that resolves to no work item is a product event — a lead answering
    from a second address, an outreach nobody recorded — and the honest place
    for it is a queryable row the owner can see, not a log line and not a 404
    the sender would learn something from. The endpoint's answer is constant
    whatever happens here; everything interesting is on this row.

    No foreign keys, exactly like `audit_events`, `action_log` and
    `work_item_transfers`: this is the record that something arrived, and
    deleting the work item, the thread or the source must not erase it.
    `owner_user_id` is stamped from the source so the row stays tenant-scopable
    on its own — which is also what lets an *unmatched* event, whose work item
    is by definition unknown, still belong to exactly one person.

    `body` holds what the sender actually sent, untouched. Sanitising on the way
    in would make the audit a record of what we decided to keep rather than of
    what we received; the scrubbing and the fencing happen in
    `services.inbound.render_untrusted`, on the way to the model, every time.
    """

    __tablename__ = "inbound_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    channel: Mapped[str] = mapped_column(Text, default="")
    #: The normalised `work_item_keys.value` this was resolved against.
    address: Mapped[str] = mapped_column(Text, default="")
    #: The sender's own id for the message, when it gave one. Deduplicates a
    #: retried delivery and a re-poll of the same mailbox.
    external_id: Mapped[str] = mapped_column(Text, default="")
    #: sha256 of the presented signature (webhook) or of the fetched record
    #: (poll). The replay guard: the same delivery twice is one event.
    delivery_hash: Mapped[str] = mapped_column(Text, default="")
    via: Mapped[str] = mapped_column(Text, default="webhook")
    #: `matched` | `ambiguous` | `unmatched` | `unroutable` | `failed`.
    status: Mapped[str] = mapped_column(Text, default="unmatched")
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: Every candidate `resolve_by_key` returned, in its order, when there was
    #: more than one. The item actually picked is `work_item_id`, which is
    #: always `candidate_ids[0]` — recorded so a wrong guess is visible.
    candidate_ids: Mapped[list] = mapped_column(JSONB, default=list)
    thread_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    #: When the owning bot was actually woken. NULL means nothing has run yet.
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StandingApproval(Base):
    """One standing permission: *don't ask me again for this button*.

    The owner approved a click and wrote "don't ask again for this button" in
    the note field. Nothing read it, and the next identical click asked again.
    A row here is what reads it — and it is a grant of authority, so every field
    on it exists to make the grant defensible rather than merely convenient.

    **What it permits is deliberately tiny.** One `action`, on one control
    identified by the role and accessible name Chrome computed (`ref_role`,
    `ref_name`), on one page (`url_key`), for one bot, granted by one human
    (`owner_user_id`). `button "Message"` on a lead's LinkedIn profile does not
    cover `button "Message"` anywhere else, because the page is part of the key
    — see `services.standing_approvals.url_key`, which normalises exactly the
    way `browser._same_page` compares.

    **No foreign keys**, like `audit_events`, `action_log`,
    `work_item_transfers` and `inbound_events`. A standing permission is
    precisely the kind of record that must survive the deletion of its subject:
    if deleting the bot erased the grant and its provenance, the answer to "who
    authorised these forty sends" would be nobody.

    **Provenance is not optional.** `origin` is `note` (the person asked, in
    words, and `note_text` is what they wrote) or `repetition` (they said yes to
    this identical thing `REPETITION_THRESHOLD` times running).
    `source_approval_ids` names the approvals either way. A database CHECK makes
    a row with no traceable origin unwritable, so "the bot decided to stop
    asking" cannot be the answer to an auditor.

    **Money and destruction are never learned.** A CHECK refuses `spend` and
    `delete` outright; `LEARNABLE_RISKS` is narrower still. The limit is stated
    in the UI rather than discovered, so it reads as a boundary rather than a
    surprise.

    Revocation sets `revoked_at`; nothing is deleted, because "what did this bot
    have permission to do in March" has to stay answerable. A partial unique
    index over the live rows is what makes at most one rule exist per identity,
    so a lookup finding two is impossible rather than merely unexpected.
    """

    __tablename__ = "standing_approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The human whose consent this is. A rule never applies to anyone else.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    bot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    #: `browser_click`, `browser_type`, `browser_select` — one of
    #: `browser.BROWSER_TARGETED`, the ops whose target is classified.
    action: Mapped[str] = mapped_column(Text)
    #: The class the gate assessed when the grant was made. Kept so a later
    #: reclassification of the same control does not silently inherit consent.
    risk: Mapped[str] = mapped_column(Text)
    ref_role: Mapped[str] = mapped_column(Text)
    ref_name: Mapped[str] = mapped_column(Text)
    #: scheme + host + path. Query and fragment dropped — a search page that
    #: re-sorted itself is the same page; another host never is.
    url_key: Mapped[str] = mapped_column(Text)
    #: `note` | `repetition`.
    origin: Mapped[str] = mapped_column(Text)
    #: Exactly what the person wrote, when `origin` is `note`. Verbatim: a
    #: paraphrase of the sentence that granted a permission is not evidence.
    note_text: Mapped[str] = mapped_column(Text, default="")
    #: The approval ids this was learned from, oldest first.
    source_approval_ids: Mapped[list] = mapped_column(JSONB, default=list)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
