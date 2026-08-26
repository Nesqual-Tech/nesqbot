/**
 * One typed function per route in docs/API.md (v0.2).
 * Nothing in here holds state — hooks compose these.
 */
import { parseSseEvent, parseThreadEvent } from "@nesqbot/protocol"
import { del, get, patch, post, request, type Query } from "./client"
import { openSse, type SseHandle } from "./sse"
import { AWAITING_HUMAN } from "../lib/takeover"
import type {
  Approval,
  ApprovalDecisionInput,
  ApprovalDecisionResult,
  AuditEvent,
  BindConnectorInput,
  Bot,
  BotDesktop,
  BudgetInput,
  Connector,
  ConnectorActionOutcome,
  ConnectorBinding,
  ConnectorStatus,
  CreateBotInput,
  CreateRoutineInput,
  CreateThreadInput,
  DeepHealthOut,
  DesktopActionInput,
  DesktopActionOutcome,
  DesktopScreenshot,
  DesktopStreamTicket,
  DesktopWindowsResult,
  EvalCase,
  EvalResult,
  EvalSuiteResult,
  HealthOut,
  KbArticle,
  McpCallInput,
  McpServer,
  McpToolsResult,
  Memory,
  Message,
  ParkedRun,
  PendingApprovalOut,
  ProviderCredentialOut,
  ProviderCredentialsOut,
  ProvidersOut,
  RegisterConnectorInput,
  RegisterMcpInput,
  ResumeRunInput,
  ResumeRunOut,
  Routine,
  Run,
  RoutineRun,
  RoutineRunStart,
  SendMessageInput,
  SendMessageResult,
  SetProviderCredentialRequest,
  StandingApproval,
  StandingApprovalList,
  TeachRoutineInput,
  Thread,
  ThreadEvent,
  TurnStreamEvent,
  TokenOut,
  UpdateBotInput,
  UpdateMcpInput,
  UpdateRoutineInput,
  UsageRow,
  User,
} from "../types"

/* ----------------------------------------------------------- health / auth */

export const getHealth = (signal?: AbortSignal) => get<HealthOut>("/health", undefined, signal)
export const getDeepHealth = (signal?: AbortSignal) => get<DeepHealthOut>("/health/deep", undefined, signal)
export const devLogin = () => post<TokenOut>("/auth/dev-login")

/**
 * Exchanges a Microsoft Entra **access token** for a Nesq Bot session token.
 *
 * The token goes in the `Authorization` header, which is where the API reads a
 * bearer credential from. It must be an access token audienced to the API and
 * carrying `access_as_user`; an ID token is rejected (see docs/entra-setup.md).
 *
 * `auth/index.tsx` calls this with the token `auth/entra.ts` obtains. A 401 here
 * is Entra's token being refused, which is what renewal is testing for, so it
 * must not re-enter the global unauthorized handler.
 */
export const entraLogin = (entraAccessToken: string) =>
  request<TokenOut>("/auth/entra", {
    method: "POST",
    headers: { Authorization: `Bearer ${entraAccessToken}` },
    noAuthRetry: true,
  })
export const getMe = (signal?: AbortSignal) => get<User>("/me", undefined, signal)

/* -------------------------------------------------------------------- bots */

export const listBots = (signal?: AbortSignal) => get<Bot[]>("/bots", undefined, signal)
export const createBot = (input: CreateBotInput) => post<Bot>("/bots", input)
export const getBot = (botId: string, signal?: AbortSignal) => get<Bot>(`/bots/${botId}`, undefined, signal)
export const updateBot = (botId: string, input: UpdateBotInput) => patch<Bot>(`/bots/${botId}`, input)
export const deleteBot = (botId: string) => del<void>(`/bots/${botId}`)
export const getProviders = (signal?: AbortSignal) => get<ProvidersOut>("/bots/providers", undefined, signal)
export const reseedSystemBots = () => post<{ ok: boolean; detail?: string }>("/bots/system/reseed")
export const listProviderCredentials = (signal?: AbortSignal) =>
  get<ProviderCredentialsOut>("/bots/providers/credentials", undefined, signal)
export const setProviderCredential = (provider: string, input: SetProviderCredentialRequest) =>
  request<ProviderCredentialOut>(`/bots/providers/${provider}/credential`, { method: "PUT", body: input })
export const deleteProviderCredential = (provider: string) => del<void>(`/bots/providers/${provider}/credential`)

/* -------------------------------------------------------- threads/messages */

export const listThreads = (signal?: AbortSignal) => get<Thread[]>("/threads", undefined, signal)
export const createThread = (input: CreateThreadInput) => post<Thread>("/threads", input)
export const deleteThread = (threadId: string) => del<void>(`/threads/${threadId}`)
export const listMessages = (threadId: string, signal?: AbortSignal) =>
  get<Message[]>(`/threads/${threadId}/messages`, undefined, signal)
export const sendMessage = (threadId: string, input: SendMessageInput, signal?: AbortSignal) =>
  post<SendMessageResult>(`/threads/${threadId}/messages`, input, undefined, signal)

/* ---------------------------------------------------------- thread streams */

/**
 * The two SSE channels do NOT carry the same events, so they get separate
 * parsers and separate unions. `parseSseEvent`/`parseThreadEvent` come from
 * `@nesqbot/protocol`: an unrecognised name or a payload that fails validation
 * arrives as `{event: "unknown"}` rather than being dropped or throwing, so a
 * newer server event cannot break this build.
 */

export interface TurnStreamHandlers {
  onEvent: (event: TurnStreamEvent) => void
  onOpen?: () => void
  onError?: (error: unknown, willRetry: boolean) => void
  onClose?: (reason: "done" | "aborted" | "error") => void
}

export interface ThreadEventHandlers {
  onEvent: (event: ThreadEvent) => void
  onOpen?: () => void
  onError?: (error: unknown, willRetry: boolean) => void
  onClose?: (reason: "done" | "aborted" | "error") => void
}

/** `POST /threads/{id}/messages/stream` — one chat turn, never reconnected. */
export function openMessageStream(threadId: string, input: SendMessageInput, handlers: TurnStreamHandlers): SseHandle {
  return openSse({
    path: `/threads/${threadId}/messages/stream`,
    method: "POST",
    body: input,
    reconnect: false,
    onMessage: (message) => handlers.onEvent(parseSseEvent(message.event, message.data)),
    onOpen: handlers.onOpen,
    onError: handlers.onError,
    onClose: handlers.onClose,
  })
}

/** `GET /threads/{id}/events` — worker/routine pushes, reconnects with backoff. */
export function openThreadEvents(threadId: string, handlers: ThreadEventHandlers): SseHandle {
  return openSse({
    path: `/threads/${threadId}/events`,
    method: "GET",
    reconnect: true,
    onMessage: (message) => handlers.onEvent(parseThreadEvent(message.event, message.data)),
    onOpen: handlers.onOpen,
    onError: handlers.onError,
    onClose: handlers.onClose,
  })
}

/* -------------------------------------------------------------- runs/audit */

export const listRuns = (
  query?: { thread_id?: string; bot_id?: string; status?: string; limit?: number },
  signal?: AbortSignal,
) => get<Run[]>("/runs", query as Query | undefined, signal)
export const getRun = (runId: string, signal?: AbortSignal) => get<Run>(`/runs/${runId}`, undefined, signal)
export const listAudit = (
  query?: { bot_id?: string; event_type?: string; limit?: number; before?: string },
  signal?: AbortSignal,
) => get<AuditEvent[]>("/audit", query as Query | undefined, signal)

/* --------------------------------------------------------------- approvals */

export const listApprovals = (query?: { status?: string; bot_id?: string }, signal?: AbortSignal) =>
  get<Approval[]>("/approvals", query as Query | undefined, signal)
export const getApproval = (id: string, signal?: AbortSignal) => get<Approval>(`/approvals/${id}`, undefined, signal)
export const decideApproval = (id: string, input: ApprovalDecisionInput) =>
  post<ApprovalDecisionResult>(`/approvals/${id}/decide`, input)
export const expireApproval = (id: string) => post<Approval>(`/approvals/${id}/expire`)

/**
 * Standing permissions — *"don't ask again for this button"*.
 *
 * Same slice of the contract as the queue above, deliberately: what is waiting
 * on you and what you have already allowed are two halves of one question.
 */
export const listStandingApprovals = (query?: { include_revoked?: boolean }, signal?: AbortSignal) =>
  get<StandingApprovalList>("/standing-approvals", query as Query | undefined, signal)
export const revokeStandingApproval = (id: string) => post<StandingApproval>(`/standing-approvals/${id}/revoke`)

/* -------------------------------------------------------------- connectors */

export const listConnectors = (signal?: AbortSignal) => get<Connector[]>("/integrations/connectors", undefined, signal)
export const registerConnector = (input: RegisterConnectorInput) => post<Connector>("/integrations/connectors", input)
export const deleteConnector = (connectorId: string) => del<void>(`/integrations/connectors/${connectorId}`)
export const listBotConnectors = (botId: string, signal?: AbortSignal) =>
  get<ConnectorBinding[]>(`/bots/${botId}/connectors`, undefined, signal)
export const bindConnector = (botId: string, connectorId: string, input: BindConnectorInput) =>
  post<{ ok?: boolean; status?: ConnectorStatus }>(`/bots/${botId}/connectors/${connectorId}`, input)
export const unbindConnector = (botId: string, connectorId: string) =>
  del<void>(`/bots/${botId}/connectors/${connectorId}`)
/**
 * Executes the action, or returns 201 `PendingApprovalOut` when the risk class
 * requires sign-off. Narrow the result with `isPendingApproval` before reading
 * it — the two shapes have nothing in common.
 */
export const executeConnectorAction = (
  botId: string,
  connectorId: string,
  action: string,
  input: Record<string, unknown>,
) => post<ConnectorActionOutcome>(`/bots/${botId}/connectors/${connectorId}/actions/${action}`, input)

/** Narrows a risk-gated 201 response apart from an executed action result. */
export function isPendingApproval(value: unknown): value is PendingApprovalOut {
  if (typeof value !== "object" || value === null) return false
  const candidate = value as { status?: unknown; approval_id?: unknown }
  // `status` is the reliable discriminant: `approval_id` is nullable on
  // PendingApprovalResponse, so its absence does not mean "executed".
  return candidate.status === "pending_approval" || typeof candidate.approval_id === "string"
}

/* --------------------------------------------------------------------- mcp */

export const listMcp = (signal?: AbortSignal) => get<McpServer[]>("/integrations/mcp", undefined, signal)
export const registerMcp = (input: RegisterMcpInput) => post<McpServer>("/integrations/mcp", input)
export const updateMcp = (id: string, input: UpdateMcpInput) => patch<McpServer>(`/integrations/mcp/${id}`, input)
export const deleteMcp = (id: string) => del<void>(`/integrations/mcp/${id}`)

/** `{mcp_id, name, tools, mock, error}` — the tools live under `.tools`. */
export async function listMcpTools(id: string, signal?: AbortSignal): Promise<McpToolsResult> {
  const result = await get<McpToolsResult>(`/integrations/mcp/${id}/tools`, undefined, signal)
  return { ...result, tools: result?.tools ?? [] }
}

export const attachMcp = (botId: string, mcpId: string) => post<{ ok?: boolean }>(`/bots/${botId}/mcp/${mcpId}`)
export const detachMcp = (botId: string, mcpId: string) => del<void>(`/bots/${botId}/mcp/${mcpId}`)
export const callMcpTool = (botId: string, mcpId: string, input: McpCallInput) =>
  post<unknown>(`/bots/${botId}/mcp/${mcpId}/call`, input)

/* ------------------------------------------------------------- bot desktop */

export const getDesktop = (botId: string, signal?: AbortSignal) =>
  get<BotDesktop>(`/bots/${botId}/desktop`, undefined, signal)
export const startDesktop = (botId: string) => post<BotDesktop>(`/bots/${botId}/desktop/start`)
export const stopDesktop = (botId: string, wipe = false) =>
  post<BotDesktop>(`/bots/${botId}/desktop/stop`, undefined, { wipe })
export const suspendDesktop = (botId: string) => post<BotDesktop>(`/bots/${botId}/desktop/suspend`)
export const resumeDesktop = (botId: string) => post<BotDesktop>(`/bots/${botId}/desktop/resume`)
/** Risk-gated: `send`/`spend`/`delete` actions come back as 201 PendingApprovalOut. */
export const desktopAction = (botId: string, input: DesktopActionInput, signal?: AbortSignal) =>
  post<DesktopActionOutcome>(`/bots/${botId}/desktop/action`, input, undefined, signal)
export const desktopScreenshot = (botId: string, signal?: AbortSignal) =>
  get<DesktopScreenshot>(`/bots/${botId}/desktop/screenshot`, undefined, signal)
export const desktopWindows = (botId: string, signal?: AbortSignal) =>
  get<DesktopWindowsResult>(`/bots/${botId}/desktop/windows`, undefined, signal)

/**
 * Mint a viewing ticket for the desktop stream proxy.
 *
 * This is the only authenticated step in the viewer flow — the iframe and the
 * WebSocket that follow cannot send a bearer token, so they carry the ticket in
 * their path instead. See `desktopStreamUrl` in `lib/desktopStream`.
 */
export const createDesktopStreamTicket = (botId: string, signal?: AbortSignal) =>
  post<DesktopStreamTicket>(`/bots/${botId}/desktop/stream/ticket`, undefined, undefined, signal)

/* ---------------------------------------------------------------- routines */

export const listRoutines = (botId?: string, signal?: AbortSignal) =>
  get<Routine[]>("/routines", botId ? { bot_id: botId } : undefined, signal)
export const createRoutine = (input: CreateRoutineInput) => post<Routine>("/routines", input)
export const teachRoutine = (input: TeachRoutineInput) => post<Routine>("/routines/teach", input)
export const getRoutine = (id: string, signal?: AbortSignal) => get<Routine>(`/routines/${id}`, undefined, signal)
export const updateRoutine = (id: string, input: UpdateRoutineInput) => patch<Routine>(`/routines/${id}`, input)
export const deleteRoutine = (id: string) => del<void>(`/routines/${id}`)
export const runRoutine = (id: string) => post<RoutineRunStart>(`/routines/${id}/run`)
export const listRoutineRuns = (id: string, signal?: AbortSignal) =>
  get<RoutineRun[]>(`/routines/${id}/runs`, undefined, signal)

/* ------------------------------------------------------------- memory / kb */

export const listMemories = (botId: string, limit?: number, signal?: AbortSignal) =>
  get<Memory[]>(`/bots/${botId}/memories`, limit ? { limit } : undefined, signal)
export const createMemory = (botId: string, input: { kind: string; content: string }) =>
  post<Memory>(`/bots/${botId}/memories`, input)
export const deleteMemory = (id: string) => del<void>(`/memories/${id}`)
export const searchKb = (q: string, limit?: number, signal?: AbortSignal) =>
  get<KbArticle[]>("/kb", { q, limit }, signal)
export const createKbArticle = (input: { title: string; body: string }) => post<KbArticle>("/kb", input)
export const updateKbArticle = (id: string, input: { title?: string; body?: string }) =>
  patch<KbArticle>(`/kb/${id}`, input)
export const deleteKbArticle = (id: string) => del<void>(`/kb/${id}`)

/* -------------------------------------------------------------- usage/evals */

export const getUsage = (days = 1, signal?: AbortSignal) => get<UsageRow[]>("/usage", { days }, signal)
/** Returns the full `BotOut`, so callers can update state without a refetch. */
export const updateBudget = (botId: string, input: BudgetInput) => patch<Bot>(`/bots/${botId}/budget`, input)
export const runEval = (input: EvalCase) => post<EvalResult>("/evals/run", input)
export const runEvalSuite = (cases: EvalCase[]) => post<EvalSuiteResult>("/evals/suite", { cases })

/* ------------------------------------------------------------ human handoff */

/**
 * Every run currently parked on a person.
 *
 * This is what makes a takeover survive the app being closed: the run is
 * `awaiting_human` server-side with its agent state persisted, so re-opening
 * the app and asking this question is enough to find it again. Owner-scoped
 * like every other list.
 */
export const listAwaitingHumanRuns = (limit = 20, signal?: AbortSignal) =>
  get<ParkedRun[]>("/runs", { status: AWAITING_HUMAN, limit }, signal)

/**
 * `POST /runs/{run_id}/resume` — the "I'm done, carry on" button.
 *
 * Built from `run_id` rather than from the event's `resume_url`: that field is
 * a server-rendered absolute path (`/api/runs/…/resume`) and `API_BASE`
 * already ends in `/api`, so honouring it would either double the prefix or
 * require string surgery on a value the server is free to change. The id is
 * the stable part.
 *
 * Three answers matter to a caller:
 *
 *  - `{resumed: true}` — the agent picked the task back up.
 *  - `{resumed: false}` — **not an error.** The status update is conditional,
 *    so a second call while the first is still running loses the race and says
 *    so instead of starting a second loop. Show "already going".
 *  - 409 `run_not_resumable` — the run has no agent state to continue from.
 */
export const resumeRun = (runId: string, input?: ResumeRunInput, signal?: AbortSignal) =>
  post<ResumeRunOut>(`/runs/${runId}/resume`, input ?? {}, undefined, signal)
