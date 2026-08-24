/**
 * API vocabulary for the mobile app.
 *
 * Everything the API owns is re-exported from `@nesqbot/protocol` rather than
 * re-declared here. Hand-copying shapes into this file is exactly how the phantom
 * `done.content` field got invented and how the passive chat channel silently stopped
 * appending messages -- so the rule is: if the protocol owns a shape, this file only
 * forwards it.
 *
 * The only things declared locally are client-side glue: the union describing a
 * risk-gated route that answers with EITHER an executed result OR a held approval, and
 * the narrowing helper for it.
 */

/* -------------------------------------------------------- entities / requests */

export type {
  // entities
  Approval,
  ApprovalExecution,
  ApprovalStatus,
  ApprovalContinuation,
  Bot,
  BotDesktop,
  BotDesktopState,
  DesktopScreenshot,
  DesktopStreamTicket,
  DesktopWindow,
  Message,
  ModelTier,
  RiskClass,
  Run,
  RunStatus,
  Thread,
  User,
  Uuid,
  WorkItem,
  WorkItemKey,
  WorkItemStatus,
  WorkItemTransfer,
  WorkItemTransferResult,
  // client-vocabulary aliases (same types, wire-ish names the screens already use)
  ApprovalDecisionResult,
  DesktopActionInput,
  DesktopWindowsResult,
  DeviceRegistration,
  ExecutionResult,
  HealthOut,
  PendingApprovalOut,
  ResumeRunIn,
  ResumeRunOut,
  TokenOut,
  UsageEntry,
  UsageOut,
  // approval payloads
  ApprovalPayload,
  ApprovalPayloadKind,
  ConnectorActionPayload,
  DesktopStepsPayload,
  McpToolPayload,
  MessageOnlyPayload,
  // SSE
  ChannelEvent,
  CostEventData,
  DesktopEventData,
  DesktopEventPhase,
  DoneEventData,
  HandoffEventData,
  TakeoverEventData,
  ThreadEvent,
  TurnStreamEvent,
} from "@nesqbot/protocol"

/* ------------------------------------------------------------------- helpers */

export {
  approvalExecutionOutcome,
  describeApprovalPayload,
  doneEventText,
  isParkedRunStatus,
  isStreamClosedDone,
  isTakeoverRequested,
  parseApprovalPayload,
  parseSseEvent,
  parseThreadEvent,
} from "@nesqbot/protocol"

/* ------------------------------------------------- risk-gated action results */

/**
 * A risk-gated route (`POST /bots/{id}/desktop/action`,
 * `POST /bots/{id}/connectors/{cid}/actions/{action}`) answers with one of two shapes:
 * 201 with a held `PendingApprovalResponse`, or 200 with the executed result.
 *
 * This app used to declare that union and its narrowing helper locally. It no longer
 * does: `@nesqbot/protocol` now owns both (`ActionOutcome`, `asPendingApproval`), with
 * `ActionResult` / `ExecutedActionOut` kept there as aliases for the spelling these
 * screens already use. Two hand-written copies of one narrowing rule is precisely the
 * drift this file exists to prevent, so the copies are gone and this only forwards.
 */
export type {
  ActionOutcome,
  ActionResult,
  ConnectorActionOutcome,
  DesktopActionOutcome,
  ExecutedActionOut,
} from "@nesqbot/protocol"

export { asPendingApproval, isPendingApproval } from "@nesqbot/protocol"
