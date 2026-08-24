/**
 * HTTP client for the Nesq Bot API (see docs/API.md).
 *
 * Responsibilities kept here (and nowhere else):
 *  - resolving the base URL (settings override > app.json `extra.apiUrl` > env > default)
 *  - attaching the bearer token, or the dev header when there is no token
 *  - turning the `{detail, code}` error envelope into a typed `ApiError`
 *  - request timeouts and retrying transient network failures
 */
import Constants from "expo-constants"
import { getPreferences } from "../storage/preferences"

const FALLBACK_BASE_URL = "http://localhost:8080/api"

interface ExtraConfig {
  apiUrl?: string
  entra?: { tenantId?: string; clientId?: string; scope?: string; redirectUri?: string }
  eas?: { projectId?: string }
}

function extra(): ExtraConfig {
  return (Constants.expoConfig?.extra ?? {}) as ExtraConfig
}

export function normalizeBaseUrl(raw: string): string {
  return raw.trim().replace(/\/+$/, "")
}

/** The build-time default, before any user override from the settings screen. */
export function getDefaultApiBaseUrl(): string {
  const fromEnv = process.env.EXPO_PUBLIC_API_URL
  const raw = extra().apiUrl || fromEnv || FALLBACK_BASE_URL
  return normalizeBaseUrl(raw)
}

/** The base URL actually in use right now. */
export function getApiBaseUrl(): string {
  const override = getPreferences().apiBaseUrl
  return override ? normalizeBaseUrl(override) : getDefaultApiBaseUrl()
}

export interface EntraConfig {
  /** Directory (tenant) id — the authority the app signs in against. */
  tenantId: string
  /** The **public client** app id (`Nesq Bot`), not the API's. */
  clientId: string
  /** Full scope URI: `api://<api app id>/access_as_user`. */
  scope: string
  /** Empty means "derive it from the app scheme". */
  redirectUri: string
}

/**
 * Sign-in configuration. Every value here is public — it ships inside the app
 * bundle, so none of it may ever be a secret. See docs/entra-setup.md.
 */
export function getEntraConfig(): EntraConfig {
  const configured = extra().entra ?? {}
  return {
    tenantId: process.env.EXPO_PUBLIC_ENTRA_TENANT_ID || configured.tenantId || "",
    clientId: process.env.EXPO_PUBLIC_ENTRA_CLIENT_ID || configured.clientId || "",
    scope: process.env.EXPO_PUBLIC_ENTRA_SCOPE || configured.scope || "",
    redirectUri: process.env.EXPO_PUBLIC_ENTRA_REDIRECT_URI || configured.redirectUri || "",
  }
}

export function getEasProjectId(): string | undefined {
  const fromExtra = extra().eas?.projectId
  if (fromExtra) return fromExtra
  const easConfig = Constants.easConfig as { projectId?: string } | null | undefined
  return easConfig?.projectId
}

/* ------------------------------------------------------------------ auth token */

let accessToken: string | null = null
let unauthorizedHandler: (() => void) | null = null

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function getAccessToken(): string | null {
  return accessToken
}

/** Registered by the auth provider so a 401 anywhere forces a sign-out. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

/** Headers every request (including the SSE transport) must send. */
export function authHeaders(): Record<string, string> {
  if (accessToken) return { Authorization: "Bearer " + accessToken }
  return { "X-Nesq-Dev": "1" }
}

/* ---------------------------------------------------------------------- errors */

export interface ApiErrorBody {
  detail: string
  code: string
  request_id?: string
}

/** A structured error response from the API (`{detail, code}`). */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: string
  readonly requestId?: string

  constructor(status: number, body: ApiErrorBody) {
    super(body.detail || body.code || "HTTP " + status)
    this.name = "ApiError"
    this.status = status
    this.code = body.code || "http_error"
    this.detail = body.detail || "HTTP " + status
    this.requestId = body.request_id
  }
}

/** The request never reached the API (airplane mode, wrong host, DNS, TLS). */
export class NetworkError extends Error {
  readonly cause?: unknown
  constructor(message = "Cannot reach the Nesq Bot API.", cause?: unknown) {
    super(message)
    this.name = "NetworkError"
    this.cause = cause
  }
}

/** The request was aborted because it exceeded the timeout. */
export class TimeoutError extends NetworkError {
  constructor(ms: number) {
    super("The API did not respond within " + Math.round(ms / 1000) + "s.")
    this.name = "TimeoutError"
  }
}

/** True when retrying / showing an offline affordance makes sense. */
export function isOffline(error: unknown): boolean {
  return error instanceof NetworkError
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail
  if (error instanceof NetworkError) return error.message
  if (error instanceof Error) return error.message
  return "Something went wrong."
}

/* --------------------------------------------------------------------- request */

export type QueryValue = string | number | boolean | null | undefined

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE"
  body?: unknown
  query?: Record<string, QueryValue>
  /** Milliseconds before the request is aborted. Default 20000. */
  timeoutMs?: number
  /** Extra network-failure retries. Defaults to 2 for GET, 0 otherwise. */
  retries?: number
  signal?: AbortSignal
  headers?: Record<string, string>
}

export function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const base = getApiBaseUrl()
  const suffix = path.startsWith("/") ? path : "/" + path
  const params: string[] = []
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue
    params.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(value)))
  }
  return base + suffix + (params.length > 0 ? "?" + params.join("&") : "")
}

const DEFAULT_TIMEOUT_MS = 20000

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function parseError(res: Response): Promise<ApiError> {
  let body: ApiErrorBody = { detail: "HTTP " + res.status, code: "http_" + res.status }
  try {
    const text = await res.text()
    if (text) {
      const parsed = JSON.parse(text) as { detail?: unknown; code?: unknown; request_id?: unknown }
      let detail = body.detail
      if (typeof parsed.detail === "string") detail = parsed.detail
      else if (parsed.detail !== undefined && parsed.detail !== null) {
        detail = JSON.stringify(parsed.detail)
      }
      body = {
        detail,
        code: typeof parsed.code === "string" ? parsed.code : body.code,
        request_id: typeof parsed.request_id === "string" ? parsed.request_id : undefined,
      }
    }
  } catch {
    /* keep the generic envelope */
  }
  return new ApiError(res.status, body)
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET"
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const maxAttempts = 1 + (options.retries ?? (method === "GET" ? 2 : 0))

  let lastError: unknown
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    const abortOuter = (): void => controller.abort()
    options.signal?.addEventListener("abort", abortOuter)

    try {
      const res = await fetch(buildUrl(path, options.query), {
        method,
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
          ...authHeaders(),
          ...options.headers,
        },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
      })

      if (res.status === 401) unauthorizedHandler?.()
      if (!res.ok) throw await parseError(res)
      if (res.status === 204) return undefined as T

      const text = await res.text()
      if (!text) return undefined as T
      return JSON.parse(text) as T
    } catch (error) {
      if (error instanceof ApiError) throw error
      if (options.signal?.aborted) throw new NetworkError("Request cancelled.", error)
      lastError = controller.signal.aborted ? new TimeoutError(timeoutMs) : new NetworkError(undefined, error)
      if (attempt < maxAttempts - 1) await delay(400 * 2 ** attempt)
    } finally {
      clearTimeout(timer)
      options.signal?.removeEventListener("abort", abortOuter)
    }
  }
  throw lastError instanceof Error ? lastError : new NetworkError()
}
