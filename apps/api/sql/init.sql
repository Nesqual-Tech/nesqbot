CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  entra_oid TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS bots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL,
  system_prompt TEXT NOT NULL,
  is_system BOOLEAN NOT NULL DEFAULT false,
  daily_budget_usd NUMERIC(10,2) NOT NULL DEFAULT 5.00,
  desktop_profile TEXT NOT NULL DEFAULT 'xfce',
  owner_user_id UUID REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS threads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  owner_user_id UUID NOT NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS thread_bots (
  thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  bot_id UUID NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
  PRIMARY KEY (thread_id, bot_id)
);

CREATE TABLE IF NOT EXISTS messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  bot_id UUID REFERENCES bots(id),
  user_id UUID REFERENCES users(id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  bot_id UUID NOT NULL REFERENCES bots(id),
  status TEXT NOT NULL DEFAULT 'queued',
  temporal_workflow_id TEXT,
  context_ledger JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS approvals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID REFERENCES runs(id) ON DELETE CASCADE,
  bot_id UUID NOT NULL REFERENCES bots(id),
  risk TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  decided_by UUID REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS memories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id UUID REFERENCES bots(id) ON DELETE CASCADE,
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  embedding vector(1536),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS bot_desktops (
  bot_id UUID PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'absent',
  container_id TEXT,
  stream_url TEXT,
  control_url TEXT,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  auth TEXT NOT NULL,
  scopes JSONB NOT NULL DEFAULT '[]',
  actions JSONB NOT NULL DEFAULT '[]',
  risk_default TEXT NOT NULL DEFAULT 'observe',
  first_party BOOLEAN NOT NULL DEFAULT false,
  manifest JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS bot_connectors (
  bot_id UUID NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
  connector_id TEXT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  secret_ref TEXT,
  status TEXT NOT NULL DEFAULT 'disconnected',
  PRIMARY KEY (bot_id, connector_id)
);

CREATE TABLE IF NOT EXISTS mcp_servers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  transport TEXT NOT NULL,
  endpoint TEXT,
  command TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  tool_allowlist JSONB NOT NULL DEFAULT '[]',
  owner_user_id UUID REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS bot_mcp (
  bot_id UUID NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
  mcp_id UUID NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  PRIMARY KEY (bot_id, mcp_id)
);

CREATE TABLE IF NOT EXISTS routines (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id UUID NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  steps JSONB NOT NULL DEFAULT '[]',
  schedule_cron TEXT,
  version INT NOT NULL DEFAULT 1,
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS cost_ledger (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id UUID NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
  tier TEXT NOT NULL,
  input_tokens INT NOT NULL DEFAULT 0,
  output_tokens INT NOT NULL DEFAULT 0,
  cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS audit_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_user_id UUID,
  bot_id UUID,
  event_type TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS kb_articles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  embedding vector(1536),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS context_ledger (
  thread_id UUID PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
  data JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cost_bot_day ON cost_ledger(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
-- ---------------------------------------------------------------------------
-- Idempotent upgrades for databases created before these columns existed.
-- Everything below is safe to re-run on every boot.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS user_devices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token TEXT NOT NULL,
  platform TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT uq_user_devices_user_token UNIQUE (user_id, token)
);

ALTER TABLE memories     ADD COLUMN IF NOT EXISTS embedding vector(1536);
ALTER TABLE kb_articles  ADD COLUMN IF NOT EXISTS embedding vector(1536);
ALTER TABLE routines     ADD COLUMN IF NOT EXISTS schedule_id TEXT;
ALTER TABLE approvals    ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE approvals    ADD COLUMN IF NOT EXISTS execution JSONB NOT NULL DEFAULT '{}';
ALTER TABLE runs         ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE runs         ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
-- Per-bot model override. NULL (both) means "use the router tier routing" -
-- see Bot.model_provider in models.py and ModelRouter.chat(bot=...).
ALTER TABLE bots         ADD COLUMN IF NOT EXISTS model_provider TEXT;
ALTER TABLE bots         ADD COLUMN IF NOT EXISTS model_name TEXT;

CREATE INDEX IF NOT EXISTS idx_user_devices_user ON user_devices(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_bot_user ON memories(bot_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_thread ON runs(thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);

-- Approvals can be created outside a chat run (routine steps of type "approval").
ALTER TABLE approvals ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE runs      ADD COLUMN IF NOT EXISTS detail JSONB NOT NULL DEFAULT '{}';

-- Routine runs are not attached to a chat thread and link back to the routine.
ALTER TABLE runs ALTER COLUMN thread_id DROP NOT NULL;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS routine_id UUID REFERENCES routines(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_runs_routine ON runs(routine_id, created_at DESC);

-- Routine ownership: lets a cron-fired schedule attribute its approvals to a
-- human. Nullable on purpose — an unattended routine is a valid state.
ALTER TABLE routines ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_routines_owner ON routines(owner_user_id);

-- Runs and approvals are audit records: they must outlive the thread that
-- produced them. Deleting a thread nulls the link instead of cascading, so a
-- queued send/spend/delete approval can be expired with a reason (see
-- DELETE /threads/{thread_id}) rather than silently disappearing.
-- Both blocks are re-runnable: they look the constraint up first and only
-- rewrite it when its delete rule is not already SET NULL.
DO $$
DECLARE
  con_name text;
  del_rule text;
BEGIN
  SELECT tc.constraint_name, rc.delete_rule
    INTO con_name, del_rule
    FROM information_schema.table_constraints AS tc
    JOIN information_schema.key_column_usage AS kcu
      ON kcu.constraint_schema = tc.constraint_schema
     AND kcu.constraint_name = tc.constraint_name
    JOIN information_schema.referential_constraints AS rc
      ON rc.constraint_schema = tc.constraint_schema
     AND rc.constraint_name = tc.constraint_name
   WHERE tc.constraint_type = 'FOREIGN KEY'
     AND tc.table_schema = current_schema()
     AND tc.table_name = 'runs'
     AND kcu.column_name = 'thread_id'
   LIMIT 1;

  IF con_name IS NOT NULL AND del_rule IS DISTINCT FROM 'SET NULL' THEN
    EXECUTE format('ALTER TABLE runs DROP CONSTRAINT %I', con_name);
    con_name := NULL;
  END IF;

  IF con_name IS NULL THEN
    ALTER TABLE runs
      ADD CONSTRAINT runs_thread_id_fkey
      FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL;
  END IF;
END
$$;

DO $$
DECLARE
  con_name text;
  del_rule text;
BEGIN
  SELECT tc.constraint_name, rc.delete_rule
    INTO con_name, del_rule
    FROM information_schema.table_constraints AS tc
    JOIN information_schema.key_column_usage AS kcu
      ON kcu.constraint_schema = tc.constraint_schema
     AND kcu.constraint_name = tc.constraint_name
    JOIN information_schema.referential_constraints AS rc
      ON rc.constraint_schema = tc.constraint_schema
     AND rc.constraint_name = tc.constraint_name
   WHERE tc.constraint_type = 'FOREIGN KEY'
     AND tc.table_schema = current_schema()
     AND tc.table_name = 'approvals'
     AND kcu.column_name = 'run_id'
   LIMIT 1;

  IF con_name IS NOT NULL AND del_rule IS DISTINCT FROM 'SET NULL' THEN
    EXECUTE format('ALTER TABLE approvals DROP CONSTRAINT %I', con_name);
    con_name := NULL;
  END IF;

  IF con_name IS NULL THEN
    ALTER TABLE approvals
      ADD CONSTRAINT approvals_run_id_fkey
      FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL;
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Rehearsal and reversibility (roadmap phase 1).
--
-- `plan_records` persists a dry run so a human can approve *the plan* and have
-- execution verify, via `content_hash`, that it did not drift in between.
-- `action_log` records every executed outbound effect together with the
-- compensating call that undoes it, or an honest reason why none exists.
--
-- Neither table carries foreign keys, matching `audit_events`: both are
-- records of what happened and must outlive the bot, run or routine that
-- produced them — and an ad-hoc single-action plan has no routine row at all.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plan_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id UUID NOT NULL,
  routine_id UUID,
  created_by UUID,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  content_hash TEXT NOT NULL,
  steps JSONB NOT NULL DEFAULT '[]',
  plan JSONB NOT NULL DEFAULT '{}',
  steps_total INTEGER NOT NULL DEFAULT 0,
  gated_steps INTEGER NOT NULL DEFAULT 0,
  failing_steps INTEGER NOT NULL DEFAULT 0,
  executed_run_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  executed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS action_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bot_id UUID NOT NULL,
  run_id UUID,
  approval_id UUID,
  actor_user_id UUID,
  kind TEXT NOT NULL,
  connector_id TEXT,
  mcp_id UUID,
  action TEXT NOT NULL,
  risk TEXT NOT NULL DEFAULT 'observe',
  target_ref TEXT,
  input_data JSONB NOT NULL DEFAULT '{}',
  result_summary JSONB NOT NULL DEFAULT '{}',
  ok BOOLEAN NOT NULL DEFAULT true,
  reversible BOOLEAN NOT NULL DEFAULT false,
  irreversible_reason TEXT,
  compensator JSONB NOT NULL DEFAULT '{}',
  undone BOOLEAN NOT NULL DEFAULT false,
  undone_at TIMESTAMPTZ,
  undone_by UUID,
  undo_result JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Re-runnable column adds, for databases created before a field existed.
ALTER TABLE plan_records ADD COLUMN IF NOT EXISTS executed_run_id UUID;
ALTER TABLE plan_records ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ;
ALTER TABLE action_log   ADD COLUMN IF NOT EXISTS risk TEXT NOT NULL DEFAULT 'observe';
ALTER TABLE action_log   ADD COLUMN IF NOT EXISTS target_ref TEXT;
ALTER TABLE action_log   ADD COLUMN IF NOT EXISTS undo_result JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_plan_records_bot ON plan_records(bot_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plan_records_routine ON plan_records(routine_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_log_bot ON action_log(bot_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_log_run ON action_log(run_id, created_at DESC);
-- The "what can I still take back?" query the UI runs.
CREATE INDEX IF NOT EXISTS idx_action_log_reversible ON action_log(bot_id, reversible, undone);

-- ---------------------------------------------------------------------------
-- Work items: owned, transferable units of work, and the ledger of who held
-- them.
--
-- A lead is the motivating case, not the mechanism, so this is one general
-- `work_items` table with a free-text `type` rather than a `leads` table plus a
-- `tickets` table plus an `invoices` table, each with its own copy of the
-- transfer ledger. The ledger is the differentiator (docs/competitive-analysis.md
-- records the competitor's audit view as "coming"), and a differentiator wants
-- exactly one place to be queried from.
--
-- `work_item_transfers` carries **no foreign keys**, matching `audit_events`,
-- `plan_records` and `action_log`: it is the record that a handover happened,
-- and deleting the item — or the bot that used to hold it — must not erase it.
-- `owner_user_id` is stamped on each row so the ledger stays tenant-scopable
-- once the work item is gone.
--
-- Every timestamp defaults to clock_timestamp(), never now(). A create writes
-- the item and its opening transfer row in one transaction, and a transaction
-- clock would stamp both identically — which is precisely the bug that broke
-- ORDER BY in `messages` and in the audit log.
--
-- Re-runnable: CREATE TABLE / CREATE INDEX / ADD COLUMN are all IF NOT EXISTS,
-- and init.sql runs on every boot.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type TEXT NOT NULL DEFAULT 'lead',
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  -- SET NULL, not CASCADE: deleting a custom bot must not delete the
  -- customer's pipeline. An item with no owning bot is a real state a human
  -- fixes with a transfer.
  owner_bot_id UUID REFERENCES bots(id) ON DELETE SET NULL,
  owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  thread_id UUID REFERENCES threads(id) ON DELETE SET NULL,
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  transferred_at TIMESTAMPTZ,
  last_event_at TIMESTAMPTZ,
  closed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS work_item_keys (
  work_item_id UUID NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  channel TEXT NOT NULL,
  value TEXT NOT NULL,
  owner_user_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (work_item_id, channel, value)
);

CREATE TABLE IF NOT EXISTS work_item_transfers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  work_item_id UUID NOT NULL,
  owner_user_id UUID NOT NULL,
  from_bot_id UUID,
  to_bot_id UUID NOT NULL,
  actor_user_id UUID,
  actor_bot_id UUID,
  reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'api',
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Re-runnable column adds, for databases created before a field existed.
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS resolution TEXT;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS transferred_at TIMESTAMPTZ;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE work_item_transfers ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'api';
ALTER TABLE work_item_transfers ADD COLUMN IF NOT EXISTS actor_bot_id UUID;

-- The owner-scoped list view: "my work items, newest first", filtered by status.
CREATE INDEX IF NOT EXISTS idx_work_items_owner ON work_items(owner_user_id, status, created_at DESC);
-- "what is this bot holding?" — the queue a bot works through, and what a
-- transfer has to re-home.
CREATE INDEX IF NOT EXISTS idx_work_items_owner_bot ON work_items(owner_bot_id, status);
-- The inbound-events seam. One exact match answers "which work item is this
-- reply about?". Deliberately not UNIQUE — see `models.WorkItemKey`.
CREATE INDEX IF NOT EXISTS idx_work_item_keys_lookup ON work_item_keys(channel, value);
CREATE INDEX IF NOT EXISTS idx_work_item_keys_owner ON work_item_keys(owner_user_id, channel, value);
-- The ledger, read newest-first per item.
CREATE INDEX IF NOT EXISTS idx_work_item_transfers_item ON work_item_transfers(work_item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_item_transfers_owner ON work_item_transfers(owner_user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Inbound events: the way the outside world reaches a bot.
--
-- Two entry shapes and deliberately one code path behind them
-- (`services.inbound.ingest`). A `webhook` source is pushed to over an
-- unauthenticated URL; a `poll` source is pulled from a connector the owner
-- already bound. Splitting them into two pipelines is how a product ends up
-- handling a reply that arrives by email and ignoring the same reply pulled
-- from a mailbox.
--
-- `inbound_sources.slug` is server-generated and unguessable: it is the public
-- path segment, so a caller-chosen value would be a cross-tenant name grab on a
-- globally unique column *and* an enumerable surface. `secret_ref` holds a
-- reference in `services.secrets` form (`env://NAME`, `kv://vault/name`) and
-- never a key — the row is served by an owner-scoped API.
--
-- `inbound_events` carries **no foreign keys**, matching `audit_events`,
-- `action_log` and `work_item_transfers`: it is the record that something
-- arrived, and deleting the work item, the thread or the source must not erase
-- it. `owner_user_id` is stamped from the source, which is what lets an
-- *unmatched* event — one whose work item is by definition unknown — still
-- belong to exactly one person instead of to nobody.
--
-- The two unique indexes below are the replay guard. A retried webhook presents
-- the same signature, and a re-poll returns the same record id; either one
-- collides and the delivery is answered as already-seen. They are UNIQUE here,
-- unlike `work_item_keys`, because a duplicate delivery genuinely *is* one
-- event — whereas two work items sharing an address genuinely are two items.
--
-- Every timestamp defaults to clock_timestamp(), never now(): one webhook
-- writes an event row and a thread message in a single transaction, and the
-- transaction clock would stamp both identically.
--
-- Re-runnable: everything is IF NOT EXISTS, and init.sql runs on every boot.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inbound_sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'webhook',
  channel TEXT NOT NULL DEFAULT 'email',
  owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bot_id UUID REFERENCES bots(id) ON DELETE SET NULL,
  bot_ids JSONB NOT NULL DEFAULT '[]',
  secret_ref TEXT,
  connector_id TEXT,
  config JSONB NOT NULL DEFAULT '{}',
  enabled BOOLEAN NOT NULL DEFAULT true,
  last_event_at TIMESTAMPTZ,
  last_polled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS inbound_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id UUID,
  owner_user_id UUID NOT NULL,
  channel TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL DEFAULT '',
  delivery_hash TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL DEFAULT 'webhook',
  status TEXT NOT NULL DEFAULT 'unmatched',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  work_item_id UUID,
  candidate_ids JSONB NOT NULL DEFAULT '[]',
  thread_id UUID,
  run_id UUID,
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  handled_at TIMESTAMPTZ
);

-- Re-runnable column adds, for databases created before a field existed.
ALTER TABLE inbound_sources ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;
ALTER TABLE inbound_sources ADD COLUMN IF NOT EXISTS last_polled_at TIMESTAMPTZ;
ALTER TABLE inbound_events  ADD COLUMN IF NOT EXISTS candidate_ids JSONB NOT NULL DEFAULT '[]';
ALTER TABLE inbound_events  ADD COLUMN IF NOT EXISTS handled_at TIMESTAMPTZ;

-- The owner-scoped inbox view: "what came in, newest first", filtered by status
-- so "show me everything that matched nothing" is one indexed query.
CREATE INDEX IF NOT EXISTS idx_inbound_events_owner ON inbound_events(owner_user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_events_item ON inbound_events(work_item_id, created_at DESC);
-- Replay: the same signature, or the same provider message id, is one event.
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_events_delivery ON inbound_events(source_id, delivery_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_events_external ON inbound_events(source_id, external_id) WHERE external_id <> '';
CREATE INDEX IF NOT EXISTS idx_inbound_sources_owner ON inbound_sources(owner_user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Standing approvals: "don't ask me again for this button".
--
-- The owner approved a click and typed *"don't ask again for this button"* into
-- the note field. Nothing read it, and the next identical click asked again.
-- This table is what reads it. A row is a **standing permission**: for one
-- person, one bot, one control, on one page, the gate in `simulation.perform`
-- records why it did not stop instead of parking the step for a decision.
--
-- **No foreign keys**, exactly like `audit_events`, `action_log`,
-- `work_item_transfers` and `inbound_events`, and for the strongest version of
-- the same reason: this is the record that a human granted a standing
-- permission. Deleting the bot it was granted over, or the approvals it was
-- learned from, must not erase the grant or its provenance. A grant that can be
-- deleted by deleting its subject is not an audit trail.
--
-- **Identity, never a label.** `ref_role` + `ref_name` are the role and the
-- accessible name Chrome computed, and `url_key` is scheme+host+path with the
-- query and fragment dropped — the same comparison `browser._same_page` makes.
-- A page that renders an attacker-chosen `button "Message"` on another host
-- does not match this row, because the host is part of the key.
--
-- **Three CHECKs, because they are the safeguards.**
--
-- * `origin_is_traceable` — every row names why it exists and which approvals
--   made it. A rule with no traceable origin is not merely discouraged, it is
--   unwritable. A `note` rule additionally has to carry the words that asked
--   for it, so "the bot decided to stop asking" is never the answer to an
--   auditor.
-- * `never_money_or_destruction` — `spend` and `delete` can never be learned.
--   Not policy in a Python constant that a later change can widen: the database
--   refuses the row. `services.standing_approvals.LEARNABLE_RISKS` is narrower
--   still (`send` alone), which is the policy; this is the floor under it.
-- * `identity_is_complete` — a rule with an empty name, an empty role or a
--   non-http(s) page has nothing to match on, and matching on nothing matches
--   everything.
--
-- Revocation is a timestamp, never a DELETE: "what did this bot have permission
-- to do last March" has to stay answerable. The partial unique index is what
-- makes at most one *live* rule exist per identity, so a lookup that finds two
-- is impossible rather than merely unexpected.
--
-- Re-runnable: everything is IF NOT EXISTS, and init.sql runs on every boot.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS standing_approvals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id UUID NOT NULL,
  bot_id UUID NOT NULL,
  action TEXT NOT NULL,
  risk TEXT NOT NULL,
  ref_role TEXT NOT NULL,
  ref_name TEXT NOT NULL,
  url_key TEXT NOT NULL,
  origin TEXT NOT NULL,
  note_text TEXT NOT NULL DEFAULT '',
  source_approval_ids JSONB NOT NULL DEFAULT '[]',
  use_count INTEGER NOT NULL DEFAULT 0,
  last_used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  revoked_at TIMESTAMPTZ,
  revoked_by UUID,
  CONSTRAINT standing_approvals_origin_is_traceable CHECK (
    origin IN ('note', 'repetition')
    AND jsonb_typeof(source_approval_ids) = 'array'
    AND jsonb_array_length(source_approval_ids) >= 1
    AND (origin <> 'note' OR length(btrim(note_text)) > 0)
  ),
  CONSTRAINT standing_approvals_never_money_or_destruction CHECK (
    risk NOT IN ('spend', 'delete')
  ),
  CONSTRAINT standing_approvals_identity_is_complete CHECK (
    length(btrim(action)) > 0
    AND length(btrim(ref_role)) > 0
    AND length(btrim(ref_name)) > 0
    AND url_key ~ '^https?://'
  )
);

-- At most one live rule per identity. The gate reads this index; two live rows
-- for one control would be a lookup with a choice to make, and choosing is the
-- thing the whole DOM lane refuses to do.
CREATE UNIQUE INDEX IF NOT EXISTS uq_standing_approvals_live
  ON standing_approvals(owner_user_id, bot_id, action, url_key, ref_role, ref_name)
  WHERE revoked_at IS NULL;
-- "What has this person granted, newest first" — the Standing permissions list.
CREATE INDEX IF NOT EXISTS idx_standing_approvals_owner
  ON standing_approvals(owner_user_id, created_at DESC);

-- Which standing permission let an executed effect through, on the row that
-- records the effect. Without it the undo log can say a `send` ran with no
-- approval behind it and offer no way to find out why. NULL is the ordinary
-- case: a human approved it, or it never needed approving.
ALTER TABLE action_log ADD COLUMN IF NOT EXISTS standing_approval_id UUID;

-- ---------------------------------------------------------------------------
-- Session token revocation.
--
-- A session JWT is normally just decoded and trusted for its full 14-day
-- life -- there was no way to end one early. `jti` (set on every token minted
-- after this table existed; older tokens simply have none and cannot be
-- revoked, which is fine, they age out) is looked up here on every request.
-- `expires_at` mirrors the `exp` claim already on the token, so the reaper
-- can drop rows for tokens that would have expired anyway, without ever
-- needing the secret to decode them.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS revoked_tokens (
  jti UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  revoked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires_at ON revoked_tokens(expires_at);

-- A model-provider API key typed into the app rather than set by an operator
-- in the backend environment itself. Additive only, never authoritative — see
-- app/services/provider_credentials.py: an env var for the same provider
-- always wins. api_key_encrypted is a Fernet token derived from JWT_SECRET,
-- never plaintext.
CREATE TABLE IF NOT EXISTS provider_credentials (
  provider TEXT PRIMARY KEY,
  api_key_encrypted TEXT NOT NULL,
  base_url TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_by_user_id UUID REFERENCES users(id)
);

-- ---------------------------------------------------------------------------
-- `now()` is the transaction clock, not the row clock.
--
-- Postgres freezes `now()` / `CURRENT_TIMESTAMP` at the start of a transaction,
-- so every row a transaction writes gets the *same* `created_at`. The API
-- writes several rows per transaction all over the place - a turn's audit
-- events, a run's action log, a model call's cost rows - and the one that bit
-- was `ORDER BY messages.created_at` returning a thread's reply *above* the
-- question it answered, because with every timestamp equal the sort has nothing
-- to order by and Postgres is free to return whatever the plan produces.
--
-- `clock_timestamp()` is the wall clock, read per row, which is what a column
-- called `created_at` has always claimed to mean. Idempotent, so a database
-- created before this runs it once and never notices again.
-- ---------------------------------------------------------------------------
DO $$
DECLARE column_row record;
BEGIN
  FOR column_row IN
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND data_type = 'timestamp with time zone'
      AND column_default = 'now()'
  LOOP
    EXECUTE format(
      'ALTER TABLE %I ALTER COLUMN %I SET DEFAULT clock_timestamp()',
      column_row.table_name, column_row.column_name
    );
  END LOOP;
END $$;
