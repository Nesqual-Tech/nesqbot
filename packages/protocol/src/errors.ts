/**
 * The error envelope.
 *
 * Every handled error answers `{detail, code}`. Unhandled errors answer 500
 * with `{detail:"internal_error", code:"internal_error", request_id}` where
 * `request_id` matches the `X-Request-Id` response header.
 */

/** Codes the API is known to emit. Open union — new codes ship without a client release. */
export type ApiErrorCode =
  | "internal_error"
  | "not_found"
  | "forbidden"
  | "unauthorized"
  | "invalid_token"
  | "validation_error"
  | "conflict"
  | "already_decided"
  | "budget_exceeded"
  | "approval_required"
  | "connector_not_bound"
  | "connector_error"
  | "mcp_unreachable"
  | "desktop_not_running"
  | "desktop_error"
  | "system_bot_immutable"
  | "temporal_unavailable"
  | "rate_limited"
  | (string & {})

export interface ApiError {
  detail: string
  code: ApiErrorCode
  /** Present on 500s. Quote it in a bug report — it is in the API logs. */
  request_id?: string
}

/** Header the API stamps on every response and echoes into 500 bodies. */
export const REQUEST_ID_HEADER = "X-Request-Id"

/** Header that bypasses auth in development. Never send it to production. */
export const DEV_AUTH_HEADER = "X-Nesq-Dev"

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function isApiError(value: unknown): value is ApiError {
  return isRecord(value) && typeof value["detail"] === "string" && typeof value["code"] === "string"
}

/**
 * Pull a displayable message out of anything a fetch might reject with.
 * Prefers the API's `detail`, falls back to an Error message, then a literal.
 */
export function apiErrorMessage(value: unknown, fallback = "Something went wrong"): string {
  if (isApiError(value)) return value.detail
  if (value instanceof Error && value.message) return value.message
  if (typeof value === "string" && value) return value
  if (isRecord(value) && typeof value["detail"] === "string") return value["detail"]
  return fallback
}
