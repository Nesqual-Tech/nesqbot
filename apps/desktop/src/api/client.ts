/**
 * Typed fetch wrapper for the Nesq Bot API.
 *
 * - resolves the base URL once (`VITE_API_URL`, default `http://localhost:8080/api`)
 * - attaches `Authorization: Bearer …` when a token is stored; in development
 *   builds only, falls back to the `X-Nesq-Dev: 1` bypass header
 * - turns the API error envelope `{detail, code, request_id?}` into `ApiError`
 * - captures the `X-Request-Id` response header on every call (and on errors)
 * - reports a 401 to the auth provider, which renews rather than signing out
 */

const BUILT_IN_API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8080/api").replace(/\/+$/, "")

/**
 * `nesq.api.base` in `localStorage` — non-secret config, same tier as the
 * mobile app's own endpoint override (`apps/mobile/src/api/client.ts`) and
 * `auth/storage.ts`'s `USER_CACHE_KEY`. Never an API key or a token: this key
 * only ever holds a URL.
 */
const API_BASE_STORAGE_KEY = "nesq.api.base"

function readApiBaseOverride(): string | null {
  try {
    const raw = localStorage.getItem(API_BASE_STORAGE_KEY)
    return raw ? raw.replace(/\/+$/, "") : null
  } catch {
    return null
  }
}

/**
 * The base URL every request in this module resolves against.
 *
 * A `let`, not a `const`: `setApiBase` below mutates it, and every caller —
 * `buildUrl`, and therefore `get`/`post`/`patch`/`del` — reads this binding
 * fresh on each call rather than a value captured once at import time, so a
 * change from the setup wizard takes effect on the very next request with no
 * reload required.
 */
export let API_BASE: string = readApiBaseOverride() ?? BUILT_IN_API_BASE

/** The address this build ships with, before any override — what "Reset" in the setup wizard restores. */
export const DEFAULT_API_BASE = BUILT_IN_API_BASE

/**
 * Point every subsequent request at a different backend.
 *
 * `persist: false` is for a connectivity probe during setup — try it, but do
 * not commit to it until the caller confirms `/health` actually answers.
 * `url: null` clears the override and reverts to `DEFAULT_API_BASE`.
 */
export function setApiBase(url: string | null, options: { persist?: boolean } = {}): void {
  const { persist = true } = options
  const next = url ? url.trim().replace(/\/+$/, "") : null
  API_BASE = next || BUILT_IN_API_BASE
  if (!persist) return
  try {
    if (next) localStorage.setItem(API_BASE_STORAGE_KEY, next)
    else localStorage.removeItem(API_BASE_STORAGE_KEY)
  } catch {
    /* private mode / disabled storage — the override still applies for this session */
  }
}

let memoryToken: string | null = null
let lastRequestId: string | null = null
let unauthorizedHandler: (() => void) | null = null

/**
 * The session token is held in memory only.
 *
 * Persistence is `auth/storage.ts`'s job, and it uses the OS credential store —
 * this module deliberately no longer writes the token to `localStorage`, which
 * is plaintext on disk and readable by any script that gets into the page.
 */
export function getToken(): string | null {
  return memoryToken
}

export function setToken(token: string | null): void {
  memoryToken = token
}

/**
 * Registered by the auth provider. A 401 means "the local session token died",
 * which is a renewal trigger, not a sign-out — see `auth/index.tsx`.
 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

/** Request id of the most recent response — handy for support tickets. */
export function getLastRequestId(): string | null {
  return lastRequestId
}

export type QueryValue = string | number | boolean | null | undefined
export type Query = Record<string, QueryValue>

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE"
  body?: unknown
  query?: Query
  signal?: AbortSignal
  headers?: Record<string, string>
  /** Skip JSON parsing and resolve with the raw Response. */
  raw?: boolean
  /**
   * Do not report a 401 to the unauthorized handler. Set on the calls that are
   * themselves part of renewal (`/auth/entra`), which would otherwise recurse.
   */
  noAuthRetry?: boolean
}

/** Error carrying the API envelope: `code`, `detail` and the request id. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: string
  readonly requestId: string | null
  readonly body: unknown

  constructor(init: { status: number; code: string; detail: string; requestId?: string | null; body?: unknown }) {
    super(init.detail || init.code || `HTTP ${init.status}`)
    this.name = "ApiError"
    this.status = init.status
    this.code = init.code
    this.detail = init.detail
    this.requestId = init.requestId ?? null
    this.body = init.body
  }

  /** Network-level failure (API down, DNS, CORS, offline). */
  get isOffline(): boolean {
    return this.status === 0
  }

  get isAuth(): boolean {
    return this.status === 401 || this.status === 403
  }

  get isConflict(): boolean {
    return this.status === 409
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError
}

export function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === "AbortError") return true
  return typeof err === "object" && err !== null && (err as { name?: string }).name === "AbortError"
}

/** Human-readable message for any thrown value. */
export function errorMessage(err: unknown): string {
  if (isApiError(err)) {
    if (err.isOffline) return "Cannot reach the Nesq Bot API. Is it running on " + API_BASE + "?"
    return err.detail || err.code || `Request failed (${err.status})`
  }
  if (err instanceof Error) return err.message
  if (typeof err === "string") return err
  return "Something went wrong."
}

/** Short machine code for any thrown value, for badges/telemetry. */
export function errorCode(err: unknown): string {
  if (isApiError(err)) return err.code
  return "client_error"
}

export function buildUrl(path: string, query?: Query): string {
  const base = path.startsWith("http") ? path : `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`
  if (!query) return base
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${base}${base.includes("?") ? "&" : "?"}${qs}` : base
}

/**
 * Whether this build may fall back to the development authentication bypass.
 *
 * `import.meta.env.DEV` is replaced with a literal at build time, so in a
 * packaged app this is `false` and the branch below is eliminated entirely —
 * the string "X-Nesq-Dev" does not appear in the shipped bundle at all.
 *
 * It used to be unconditional, and that was wrong twice over. A packaged app
 * that advertises an authentication bypass header is only safe because the
 * server happens to be configured to refuse it, which is not a property to rely
 * on. And it produced the failure the owner reported: with `NESQ_ENV=production`
 * the live API refuses the header, so every protected call made before sign-in
 * came back 401 "missing bearer token to access api", which the shell then
 * rendered as a raw error on a cold start.
 */
export const DEV_AUTH_BYPASS: boolean = import.meta.env.DEV

/**
 * True when the API will refuse an unauthenticated request — i.e. whenever we
 * have no dev bypass to fall back on. The shell uses this to decide whether to
 * put a sign-in screen in front of the app instead of firing requests that can
 * only 401.
 */
export const REQUIRES_SIGN_IN: boolean = !DEV_AUTH_BYPASS

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getToken()
  if (token) return { Authorization: `Bearer ${token}`, ...extra }
  if (DEV_AUTH_BYPASS) return { "X-Nesq-Dev": "1", ...extra }
  // No token and no bypass: send the request bare. It will 401, which is the
  // honest answer, and the auth provider turns that into a renewal attempt.
  return { ...extra }
}

function parseErrorBody(raw: string): { detail: string; code: string; requestId: string | null; body: unknown } {
  if (!raw) return { detail: "", code: "", requestId: null, body: null }
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === "object") {
      const obj = parsed as Record<string, unknown>
      const detail =
        typeof obj.detail === "string"
          ? obj.detail
          : Array.isArray(obj.detail)
            ? obj.detail.map((d) => JSON.stringify(d)).join("; ")
            : typeof obj.message === "string"
              ? obj.message
              : ""
      return {
        detail,
        code: typeof obj.code === "string" ? obj.code : "",
        requestId: typeof obj.request_id === "string" ? obj.request_id : null,
        body: parsed,
      }
    }
  } catch {
    /* fall through to text */
  }
  return { detail: raw.slice(0, 400), code: "", requestId: null, body: raw }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query, signal, headers, raw, noAuthRetry } = options
  const url = buildUrl(path, query)

  let res: Response
  try {
    res = await fetch(url, {
      method,
      signal,
      headers: authHeaders({
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      }),
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (err) {
    if (isAbortError(err)) throw err
    throw new ApiError({
      status: 0,
      code: "network_error",
      detail: err instanceof Error ? err.message : "Network request failed",
    })
  }

  const requestId = res.headers.get("X-Request-Id")
  if (requestId) lastRequestId = requestId

  if (!res.ok) {
    // Only when a session actually exists: a 401 on the `X-Nesq-Dev` path means
    // the dev header is disabled, which renewal cannot fix.
    if (res.status === 401 && !noAuthRetry && memoryToken) unauthorizedHandler?.()

    const text = await res.text().catch(() => "")
    const parsed = parseErrorBody(text)
    throw new ApiError({
      status: res.status,
      code: parsed.code || `http_${res.status}`,
      detail: parsed.detail || res.statusText || `Request failed (${res.status})`,
      requestId: parsed.requestId ?? requestId,
      body: parsed.body,
    })
  }

  if (raw) return res as unknown as T
  if (res.status === 204) return undefined as T

  const text = await res.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError({
      status: res.status,
      code: "invalid_json",
      detail: "The API returned a response that is not valid JSON.",
      requestId,
      body: text.slice(0, 400),
    })
  }
}

export const get = <T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> =>
  request<T>(path, { method: "GET", query, signal })

export const post = <T>(path: string, body?: unknown, query?: Query, signal?: AbortSignal): Promise<T> =>
  request<T>(path, { method: "POST", body, query, signal })

export const patch = <T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> =>
  request<T>(path, { method: "PATCH", body, signal })

export const del = <T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> =>
  request<T>(path, { method: "DELETE", query, signal })
