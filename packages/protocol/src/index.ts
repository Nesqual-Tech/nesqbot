/**
 * @nesqbot/protocol — the shared shape of the Nesq Bot API.
 *
 * Source of truth: `docs/API.md` (contract) and `apps/api/app/schemas.py`
 * (implementation). This package is types plus a small amount of logic that
 * both the API and the clients must agree on — risk classification, terminal
 * states, and the two discriminated unions (approval payloads, SSE events).
 * It has no runtime dependencies and nothing in it does I/O.
 */

export * from "./core"
export * from "./entities"
export * from "./approvals"
export * from "./events"
export * from "./errors"
export * from "./requests"

/* ------------------------------------------------------------------ *
 * Wire-name aliases
 *
 * The API's pydantic models are named `XxxIn` / `XxxOut`. Client code often
 * reads better against those names when it is transcribing an endpoint, so
 * each one is aliased to its domain type here. Both names are supported;
 * pick one per file and stay consistent.
 * ------------------------------------------------------------------ */

import type { ConnectorBindingStatus } from "./core"
import type {
  Approval,
  AuditEvent,
  Bot,
  BotConnectorBinding,
  BotDesktop,
  ConnectorManifest,
  ConnectorActionResult,
  ApprovalExecution,
  DeepHealth,
  DesktopScreenshot,
  DesktopStreamTicket,
  DesktopWindowList,
  Device,
  EvalCase,
  EvalSuiteResult,
  KbArticle,
  KbSearchResult,
  McpServer,
  McpToolList,
  Memory,
  Message,
  Routine,
  RoutineRunHandle,
  Run,
  Thread,
  UsageEntry,
  UsageSummary,
  User,
  Health,
  InboundAck,
  InboundEvent,
  InboundPollResult,
  InboundSource,
  WorkItem,
  WorkItemKey,
  WorkItemTransfer,
  WorkItemTransferResult,
  StandingApproval,
  StandingApprovalList,
} from "./entities"
import type {
  ActionOutcome,
  ApprovalDecisionRequest,
  BindConnectorRequest,
  CallMcpToolRequest,
  CreateApprovalRequest,
  CreateCustomBotRequest,
  CreateKbArticleRequest,
  CreateMemoryRequest,
  CreateRoutineRequest,
  CreateThreadRequest,
  DesktopActionRequest,
  EntraLoginRequest,
  ExecuteConnectorActionRequest,
  OkResponse,
  PendingApprovalResponse,
  ProviderCredentialResponse,
  ProviderCredentialsResponse,
  ProvidersResponse,
  RegisterConnectorRequest,
  RegisterDeviceRequest,
  RegisterMcpRequest,
  ResumeRunRequest,
  ResumeRunResponse,
  RunEvalSuiteRequest,
  SendMessageRequest,
  SetProviderCredentialRequest,
  TeachRoutineRequest,
  TokenResponse,
  UpdateBotRequest,
  UpdateBudgetRequest,
  UpdateKbArticleRequest,
  UpdateMcpRequest,
  UpdateRoutineRequest,
  UpdateRunStatusRequest,
  ApprovalDecisionResponse,
  SendMessageResponse,
  CreateInboundSourceRequest,
  UpdateInboundSourceRequest,
  CreateWorkItemRequest,
  UpdateWorkItemRequest,
  TransferWorkItemRequest,
} from "./requests"

export type UserOut = User
export type BotOut = Bot
export type ProvidersOut = ProvidersResponse
export type ProviderCredentialOut = ProviderCredentialResponse
export type ProviderCredentialsOut = ProviderCredentialsResponse
export type ThreadOut = Thread
export type MessageOut = Message
export type RunOut = Run
export type AuditEventOut = AuditEvent
export type ApprovalOut = Approval
export type DesktopOut = BotDesktop
export type ConnectorOut = ConnectorManifest
export type McpOut = McpServer
export type RoutineOut = Routine
export type MemoryOut = Memory
export type KbArticleOut = KbArticle
export type UsageOut = UsageSummary
export type TokenOut = TokenResponse
export type BotConnectorOut = BotConnectorBinding
export type McpToolsOut = McpToolList
export type RoutineRunOut = RoutineRunHandle
export type PendingApprovalOut = PendingApprovalResponse
export type EvalSuiteOut = EvalSuiteResult
export type HealthDeepOut = DeepHealth
export type ScreenshotOut = DesktopScreenshot
export type DesktopWindowsOut = DesktopWindowList
export type DesktopStreamTicketOut = DesktopStreamTicket
export type ResumeRunIn = ResumeRunRequest
export type ResumeRunOut = ResumeRunResponse
export type DeviceOut = Device
export type KbSearchOut = KbSearchResult
export type OkOut = OkResponse
export type UsageEntryOut = UsageEntry
export type WorkItemOut = WorkItem
export type WorkItemKeyOut = WorkItemKey
export type WorkItemTransferOut = WorkItemTransfer
export type WorkItemTransferResultOut = WorkItemTransferResult
export type WorkItemIn = CreateWorkItemRequest
export type UpdateWorkItemIn = UpdateWorkItemRequest
export type WorkItemTransferIn = TransferWorkItemRequest
export type InboundSourceOut = InboundSource
export type InboundEventOut = InboundEvent
export type InboundAckOut = InboundAck
export type InboundPollOut = InboundPollResult
export type InboundSourceIn = CreateInboundSourceRequest
export type UpdateInboundSourceIn = UpdateInboundSourceRequest
export type StandingApprovalOut = StandingApproval
export type StandingApprovalListOut = StandingApprovalList

/* ------------------------------------------------------------------ *
 * Client-facing aliases
 *
 * The desktop and mobile lanes each grew a local vocabulary before this
 * package covered their surface. These aliases let both switch over by
 * deleting their local declarations rather than renaming call sites.
 * ------------------------------------------------------------------ */

export type HealthOut = Health
export type DeepHealthOut = DeepHealth
export type Connector = ConnectorManifest
export type ConnectorStatus = ConnectorBindingStatus
export type ConnectorBinding = BotConnectorBinding
export type ExecutionResult = ApprovalExecution
export type ApprovalDecisionResult = ApprovalDecisionResponse
export type McpToolsResult = McpToolList
export type DesktopActionInput = DesktopActionRequest
export type DesktopWindowsResult = DesktopWindowList

/**
 * Mobile called the gated-action union `ActionResult` and the executed branch
 * `ExecutedActionOut`; desktop called them `DesktopActionOutcome` /
 * `ConnectorActionOutcome`. The canonical names are the `*Outcome` ones — a
 * "result" implies something ran, and half this union is the case where
 * nothing did. These aliases keep mobile's spelling working.
 */
export type ActionResult = ActionOutcome
export type ExecutedActionOut = ConnectorActionResult
export type UsageRow = UsageSummary
export type SendMessageResult = SendMessageResponse
export type RoutineRunStart = RoutineRunHandle
export type DeviceRegistration = RegisterDeviceRequest

/**
 * `GET /routines/{id}/runs` returns `RunOut` rows — routine executions are
 * ordinary runs, matched on the indexed `runs.routine_id` column OR-ed with the
 * legacy `context_ledger->>'routine_id'` key for rows written before that column
 * existed. There is no separate routine-run shape — and such a run carries
 * `thread_id: null`.
 */
export type RoutineRun = Run

export type CreateBotInput = CreateCustomBotRequest
export type UpdateBotInput = UpdateBotRequest
export type CreateThreadInput = CreateThreadRequest
export type SendMessageInput = SendMessageRequest
export type ApprovalDecisionInput = ApprovalDecisionRequest
export type RegisterConnectorInput = RegisterConnectorRequest
export type BindConnectorInput = BindConnectorRequest
export type RegisterMcpInput = RegisterMcpRequest
export type UpdateMcpInput = UpdateMcpRequest
export type McpCallInput = CallMcpToolRequest
export type CreateRoutineInput = CreateRoutineRequest
export type TeachRoutineInput = TeachRoutineRequest
export type UpdateRoutineInput = UpdateRoutineRequest
export type BudgetInput = UpdateBudgetRequest

export type CreateCustomBotIn = CreateCustomBotRequest
export type UpdateBotIn = UpdateBotRequest
export type BudgetIn = UpdateBudgetRequest
export type CreateThreadIn = CreateThreadRequest
export type SendMessageIn = SendMessageRequest
export type ApprovalDecisionIn = ApprovalDecisionRequest
export type RegisterConnectorIn = RegisterConnectorRequest
export type BindConnectorIn = BindConnectorRequest
export type RegisterMcpIn = RegisterMcpRequest
export type DesktopActionIn = DesktopActionRequest
export type RoutineIn = CreateRoutineRequest
export type TeachRoutineIn = TeachRoutineRequest
export type MemoryIn = CreateMemoryRequest
export type KbArticleIn = CreateKbArticleRequest
export type EvalCaseIn = EvalCase
export type EvalSuiteIn = RunEvalSuiteRequest
export type KbArticleUpdateIn = UpdateKbArticleRequest
export type UpdateMcpIn = UpdateMcpRequest
export type UpdateRoutineIn = UpdateRoutineRequest
export type McpCallIn = CallMcpToolRequest
export type ExecuteActionIn = ExecuteConnectorActionRequest
export type EntraLoginIn = EntraLoginRequest
export type DeviceRegisterIn = RegisterDeviceRequest
export type RunStatusIn = UpdateRunStatusRequest
export type CreateApprovalIn = CreateApprovalRequest
