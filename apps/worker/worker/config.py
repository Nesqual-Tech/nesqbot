"""Worker settings — mirrors the API's env var names so one .env drives both."""

from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- shared with apps/api/app/config.py (identical env var names) ---
    nesq_env: str = "development"
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "nesq-bot"

    # --- worker specific ---
    api_internal_url: str = "http://localhost:8080"
    api_dev_header: bool = True
    """Send `X-Nesq-Dev: 1`. Ignored in production or when a token is configured."""
    worker_api_token: str = ""
    """Service token used as `Authorization: Bearer …` when set."""

    log_level: str = "INFO"

    # Worker tuning
    worker_identity: str = ""
    worker_max_concurrent_activities: int = 20
    worker_max_concurrent_workflow_tasks: int = 40
    worker_graceful_shutdown_seconds: float = 20.0

    # HTTP
    worker_http_timeout_seconds: float = 60.0
    worker_http_connect_timeout_seconds: float = 10.0

    # Desktop readiness polling
    worker_desktop_ready_timeout_seconds: float = 300.0
    worker_desktop_poll_interval_seconds: float = 3.0

    # Approvals
    worker_approval_timeout_seconds: float = 86400.0
    """How long a human has to decide.

    Enforced by `RoutineWorkflow`'s durable timer, which cannot read settings
    itself, so `client._build_schedule` bakes this value into each schedule's
    workflow argument (`approval_timeout_seconds`). It is deliberately NOT the
    deadline of one activity attempt — that is the poll budget below."""
    worker_approval_poll_interval_seconds: float = 10.0
    worker_approval_poll_budget_seconds: float = 60.0
    """Deadline for ONE `wait_for_approval_activity` attempt. Must stay below the
    workflow's `APPROVAL_START_TO_CLOSE`, which is derived from
    `activities.APPROVAL_POLL_BUDGET_SECONDS` — the workflow passes that constant
    down explicitly, so this setting only affects direct callers."""

    # Activity timeouts used by workflows
    worker_step_timeout_seconds: float = 900.0
    worker_message_timeout_seconds: float = 300.0

    # Temporal connect retry (Temporal may not be up yet in compose)
    worker_connect_max_attempts: int = 0
    """0 = retry forever."""
    worker_connect_initial_backoff_seconds: float = 1.0
    worker_connect_max_backoff_seconds: float = 30.0

    # Boot-time schedule reconciliation
    worker_reconcile_schedules: bool = True
    worker_reconcile_timeout_seconds: float = 30.0
    """Total budget for the boot-time schedule sync. Temporal can accept a
    connection and then stall on `create_schedule`; reconcile runs *before* the
    Worker is constructed, so an unbounded sync hangs boot forever, never writes
    the health marker, and the container HEALTHCHECK restarts it into a loop —
    the same failure as commits 557cab9 (schema bootstrap) and f80af74 (bots/runs
    lock). A stale schedule is survivable; a worker that never polls is not."""

    worker_reconcile_delete_orphans: bool = False
    """Delete `routine-*` schedules the fetched routine list does not contain.

    Off by default because the list the worker can fetch is NOT an inventory.
    The worker authenticates with `worker_api_token`, which the API resolves to
    the service user (`app/auth.py:get_or_create_service_user`, documented as
    owning no bots), and `GET /routines` filters on `bot_visibility_clause`
    (`Bot.is_system OR Bot.owner_user_id == user.id`). So in production the
    worker sees only system-bot routines, and an orphan sweep deletes the
    schedules the API created for every user-owned bot on every restart. In
    development the `X-Nesq-Dev` path resolves to the dev user, who does own
    bots, which is why this never showed up locally.

    Nothing is lost by defaulting off: the API already deletes a routine's
    schedule on DELETE and on disable (`routers/routines.py`)."""

    worker_reconcile_orphan_max_fraction: float = 0.34
    """Second belt for the sweep above, when it is switched on: refuse to delete
    when the orphans exceed this fraction of the existing schedules. A filtered
    or paginated list read as authoritative shows up exactly as "most of the
    schedules are suddenly orphans", so that shape aborts the sweep instead of
    executing it."""

    worker_schedule_catchup_window_seconds: float = 600.0
    """`SchedulePolicy.catchup_window`. The SDK default, measured on the installed
    temporalio 1.9.0 by inspecting the dataclass field, is 365 days: after a
    week-long outage Temporal backfills every nominal time it missed, so a
    nightly outreach routine fires seven times in a burst, at real cost. Nothing
    downstream de-duplicates that — each fire is a distinct workflow execution
    with its own idempotency key. Ten minutes means a recovery fires a routine
    once if it is genuinely due, and never replays history."""

    worker_schedule_execution_timeout_seconds: float = 90000.0
    """`execution_timeout` on the scheduled workflow. Under
    `ScheduleOverlapPolicy.SKIP` a run that never finishes silently mutes every
    later fire: one unanswered approval at 2am does not delay the routine, it
    stops it indefinitely, with no failed run and nothing in the UI. 90000s is
    25 hours — the 24-hour approval deadline plus an hour of margin, so a run
    legitimately parked on a human still reaches its own clean `timed_out` abort
    (which records a run status) before Temporal kills it from outside."""

    worker_schedule_workflow: str = "RoutineWorkflow"
    """Workflow the Temporal Schedule targets. Must match the API's
    `services/temporal_client.py`. Set to `ScheduledRoutineWorkflow` to use the
    thin child-launching wrapper instead."""

    # Liveness marker touched by the running worker; read by the Docker HEALTHCHECK.
    worker_health_file: str = "/tmp/nesq-worker.health"
    worker_health_interval_seconds: float = 15.0
    worker_health_max_age_seconds: float = 90.0

    @property
    def api_base(self) -> str:
        return self.api_internal_url.rstrip("/") + "/api"

    @property
    def is_production(self) -> bool:
        return self.nesq_env.lower() == "production"

    def api_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.worker_api_token:
            headers["Authorization"] = f"Bearer {self.worker_api_token}"
        elif self.api_dev_header and not self.is_production:
            headers["X-Nesq-Dev"] = "1"
        return headers

    def schedule_id(self, routine_id: str) -> str:
        """Schedule id format shared with the API lane."""
        return f"routine-{routine_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


_LOG_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    """Single-line key=value structured logging on stdout."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    _LOG_CONFIGURED = True
    resolved = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, resolved, logging.INFO))
    # Temporal's own chatter stays at INFO; httpx request logs are noisy at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def kv(**fields: object) -> str:
    """Render structured fields for the log formatter above."""
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if any(ch.isspace() for ch in text):
            text = '"' + text.replace('"', "'") + '"'
        parts.append(f"{key}={text}")
    return " ".join(parts)
