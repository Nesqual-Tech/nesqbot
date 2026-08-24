/**
 * The desktop app's type surface.
 *
 * Everything the API defines now comes from `@nesqbot/protocol`, which was
 * reconciled against `apps/api/app/schemas.py` and the routers. This module
 * re-exports it so app code keeps a single import point (`../types`) and adds
 * only the handful of types that are genuinely client-side or that compose two
 * protocol types into one response union.
 *
 * Do not redeclare an API shape here. If something is missing or wrong, fix it
 * in `packages/protocol` so the mobile app gets the same correction.
 */
export * from "@nesqbot/protocol"

import type {
  ConnectorActionResult,
  DesktopActionName,
  DesktopActionResult,
  PendingApprovalOut,
  RoutineStep,
  Run,
} from "@nesqbot/protocol"

/**
 * `POST /bots/{id}/desktop/action` either runs the action or answers 201 with
 * a held approval. Narrow with `isPendingApproval` from `api/endpoints`.
 */
export type DesktopActionOutcome = DesktopActionResult | PendingApprovalOut

/** Same two-shaped answer for `POST /bots/{id}/connectors/{cid}/actions/{a}`. */
export type ConnectorActionOutcome = ConnectorActionResult | PendingApprovalOut

/**
 * `POST /bots/{id}/desktop/stream/ticket` — a short-lived capability to view
 * one bot's desktop through the API's stream proxy.
 *
 * A Bot Desktop has no public address (that is the isolation claim), so noVNC
 * is served back through the API, and neither an `<iframe src>` nor a
 * `WebSocket` handshake can carry `Authorization`. The ticket is what stands in
 * for it: it lives in the URL path so relative asset references inherit it, and
 * the WebSocket that redeems it burns it.
 *
 * `streamPath`/`wsPath` are relative to the API base, not to the host.
 *
 * Declared here rather than in `packages/protocol` only because this lane does
 * not own that package; it belongs there next to `BotDesktop`, and the mobile
 * app will want it the moment it grows a viewer.
 */
export interface DesktopStreamTicket {
  ticket: string
  expires_at: string
  expires_in: number
  stream_path: string
  ws_path: string
  vnc_password: string | null
}

/**
 * A step held in the recorder's client-side list, before it is sent.
 *
 * Distinct from the protocol's `RecordedStep` (the wire payload) because the
 * UI needs a stable identity and a capture time for every row: `uid` keys the
 * list and drives reorder/delete, `at` preserves capture order. Both are
 * optional on the wire type, so the recorder cannot use it directly.
 */
export interface RecorderStep {
  uid: string
  at: number
  type: RoutineStep["type"]
  action: DesktopActionName
  x?: number
  y?: number
  text?: string
  button?: string
  keys?: string[]
  label?: string
}

/* ------------------------------------------------------------------ *
 * Human handoff — the `takeover` event and `POST /runs/{id}/resume`
 *
 * Declared here rather than in `packages/protocol` for exactly the reason
 * `DesktopStreamTicket` above is: this lane does not own that package. They
 * belong next to `ThreadEvent` and `Run` the moment someone does — the mobile
 * app will want the same three shapes as soon as it grows a resume button.
 * ------------------------------------------------------------------ */

/**
 * `takeover` on either thread channel. The agent drove until it hit something
 * only a person can do, parked the run in `awaiting_human`, and is telling you
 * what it needs.
 *
 * Not in `ThreadEvent`/`TurnStreamEvent` yet, so `parseThreadEvent` hands it
 * back as the `unknown` arm — which is precisely what that arm is for. Use
 * `parseTakeoverEvent` from `lib/takeover` to narrow it; do not switch on the
 * raw name in two places.
 */
export interface TakeoverEventData {
  /** `"requested"` is the only phase the API documents. Anything else clears. */
  phase: string
  run_id: string
  thread_id?: string | null
  bot_id?: string | null
  bot_name?: string | null
  /** Why it stopped, in the bot's words: "LinkedIn is asking for a password". */
  reason?: string | null
  /** What the human has to do: "Sign in, then press continue". */
  what_you_need?: string | null
  /** Server-supplied, and deliberately not used to build the request — see
   *  `resumeRun` in `api/endpoints`. Kept so it can be logged. */
  resume_url?: string | null
}

/** Body of `POST /runs/{run_id}/resume`. */
export interface ResumeRunInput {
  /** Free text for the transcript — "logged in as avery@…". Never a secret. */
  note?: string
}

/**
 * `POST /runs/{run_id}/resume`.
 *
 * `resumed: false` is **not** a failure. The endpoint is idempotent via a
 * conditional status update, so a second call that loses the race says so
 * rather than starting a second agent loop. Render it as "already going".
 */
export interface ResumeRunOut {
  ok: boolean
  resumed: boolean
  run_id: string
  status: string
  detail?: string | null
  message?: string | null
  outcome?: string | null
  approval_id?: string | null
  cost_usd?: number | null
}

/**
 * A run parked on a human.
 *
 * `RunStatus` in `@nesqbot/protocol` predates this state and does not list
 * `awaiting_human`, so a plain `Run` cannot express one and `run.status ===
 * "awaiting_human"` does not typecheck against it. Widening here keeps the
 * comparison honest without editing a package this lane does not own.
 */
export type ParkedRunStatus = Run["status"] | "awaiting_human"

export interface ParkedRun extends Omit<Run, "status"> {
  status: ParkedRunStatus
}
