/**
 * Held-action payloads.
 *
 * When a bot reaches an action whose risk class requires approval, the
 * orchestrator does not execute it. It serialises everything needed to run the
 * action later into `Approval.payload` and parks the run in
 * `awaiting_approval`. On `POST /approvals/{id}/decide` with `approved`, the
 * API replays that payload through `services.approvals.execute_approved(...)`.
 *
 * The payload is therefore a contract between the orchestrator (writer) and
 * the approval executor (reader) — the clients in between only render it.
 */

import type { DesktopProfile, Uuid } from "./core"

export type ApprovalPayloadKind = "connector_action" | "mcp_tool" | "desktop_steps" | "message_only"

export const APPROVAL_PAYLOAD_KINDS = [
  "connector_action",
  "mcp_tool",
  "desktop_steps",
  "message_only",
] as const satisfies readonly ApprovalPayloadKind[]

interface ApprovalPayloadCommon {
  /** Thread the action belongs to, so the client can deep-link back. */
  thread_id?: Uuid | null
  /** Human-readable rendering of what will happen. Show this, not the JSON. */
  draft?: string | null
}

/** Execute one action on a bound connector. */
export interface ConnectorActionPayload extends ApprovalPayloadCommon {
  kind: "connector_action"
  connector_id: string
  action: string
  input: Record<string, unknown>
}

/** Call one tool on an attached MCP server. */
export interface McpToolPayload extends ApprovalPayloadCommon {
  kind: "mcp_tool"
  mcp_id: Uuid
  tool: string
  arguments: Record<string, unknown>
}

/**
 * What the held action is, in the words the chat reply already uses.
 *
 * *"on approval i would like to see what the agent is trying to do, the message
 * it's trying to send, not payloads."* — the product owner, watching the queue.
 *
 * Written by `orchestrator.held_action_in_plain_words` from the arguments that
 * actually reached the chokepoint, never from what the model said it would do.
 * Absent on rows written before it existed, so a client must fall back to
 * rendering `steps` — see `ApprovalCard`.
 *
 * The tenses are a contract, not a style. `intent` is what the action *will*
 * do and is never past tense, because the whole point of the gate is that it
 * has not happened. `leading_up_to` is past tense, because those steps did.
 */
export interface HeldActionInPlainWords {
  /** `click "Message"` — present tense, always. */
  intent: string
  /** `linkedin.com/in/andrei-pop`, or empty when no page was recorded. */
  place: string
  /** `it sends something out on your behalf`. */
  why: string
  /** `Click "Message" on linkedin.com/in/andrei-pop`. Mirrors `Approval.title`. */
  title: string
  /** Markdown. Mirrors `Approval.summary`. */
  summary: string
  /**
   * The message this run typed, when it typed one on this page.
   *
   * Deliberately *what was typed*, not what will be sent: the send has not
   * happened, and a card that promises what will go out is making a claim the
   * gate exists to withhold. Null is a perfectly ordinary answer.
   */
  message?: HeldMessage | null
  /** The steps that got here, past tense, oldest first. */
  leading_up_to: string[]
}

/** The text the run typed, and the field it went into. */
export interface HeldMessage {
  text: string
  /** `"Write a message…"` — the field's accessible name, quoted. */
  into: string
  /** True when `text` was clipped for storage. */
  truncated?: boolean
}

/** Replay a recorded sequence of Bot Desktop steps. */
export interface DesktopStepsPayload extends ApprovalPayloadCommon {
  kind: "desktop_steps"
  bot_id?: Uuid
  profile?: DesktopProfile
  steps: DesktopStep[]
  /** Absent on rows written before the plain-language pass. */
  plain?: HeldActionInPlainWords | null
}

/**
 * Nothing to execute but the message itself — an outbound draft the human
 * must sign off before it leaves the building.
 */
export interface MessageOnlyPayload extends ApprovalPayloadCommon {
  kind: "message_only"
  draft: string
  /** Recipient hint, when the draft is addressed. */
  to?: string | null
}

export type ApprovalPayload = ConnectorActionPayload | McpToolPayload | DesktopStepsPayload | MessageOnlyPayload

/** One low-level Bot Desktop interaction, matching the sidecar's `/action`. */
export interface DesktopStep {
  action: DesktopActionName
  x?: number | null
  y?: number | null
  text?: string | null
  button?: "left" | "right" | "middle" | null
  keys?: string[]
}

export type DesktopActionName =
  | "click"
  | "double_click"
  | "right_click"
  | "move"
  | "type"
  | "key"
  | "scroll"
  | "screenshot"
  | (string & {})

/* ------------------------------------------------------------------ *
 * Guards
 * ------------------------------------------------------------------ */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function isApprovalPayloadKind(value: unknown): value is ApprovalPayloadKind {
  return typeof value === "string" && (APPROVAL_PAYLOAD_KINDS as readonly string[]).includes(value)
}

export function isConnectorActionPayload(value: unknown): value is ConnectorActionPayload {
  return (
    isRecord(value) &&
    value["kind"] === "connector_action" &&
    typeof value["connector_id"] === "string" &&
    typeof value["action"] === "string"
  )
}

export function isMcpToolPayload(value: unknown): value is McpToolPayload {
  return (
    isRecord(value) &&
    value["kind"] === "mcp_tool" &&
    typeof value["mcp_id"] === "string" &&
    typeof value["tool"] === "string"
  )
}

export function isDesktopStepsPayload(value: unknown): value is DesktopStepsPayload {
  return isRecord(value) && value["kind"] === "desktop_steps" && Array.isArray(value["steps"])
}

export function isMessageOnlyPayload(value: unknown): value is MessageOnlyPayload {
  return isRecord(value) && value["kind"] === "message_only" && typeof value["draft"] === "string"
}

export function isApprovalPayload(value: unknown): value is ApprovalPayload {
  return (
    isConnectorActionPayload(value) ||
    isMcpToolPayload(value) ||
    isDesktopStepsPayload(value) ||
    isMessageOnlyPayload(value)
  )
}

/**
 * Narrow an `Approval.payload` (typed as loose JSONB) to the union.
 * Returns `null` for legacy rows that predate the `kind` discriminator, so
 * callers can fall back to rendering `Approval.summary`.
 */
export function parseApprovalPayload(payload: unknown): ApprovalPayload | null {
  return isApprovalPayload(payload) ? payload : null
}

/** Best-effort one-line description for an approval card. */
export function describeApprovalPayload(payload: unknown): string | null {
  const parsed = parseApprovalPayload(payload)
  if (!parsed) return null
  switch (parsed.kind) {
    case "connector_action":
      return `${parsed.connector_id}.${parsed.action}`
    case "mcp_tool":
      return `mcp:${parsed.tool}`
    case "desktop_steps":
      return `${parsed.steps.length} desktop step${parsed.steps.length === 1 ? "" : "s"}`
    case "message_only":
      return parsed.to ? `message to ${parsed.to}` : "message"
  }
}
