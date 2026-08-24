/**
 * Where a push notification should take the person.
 *
 * Kept in its own module, apart from `./index`, for one reason: `./index` pulls
 * in `expo-device`, `expo-notifications` and the API client, so nothing outside
 * a running app can load it — and this is the piece most worth checking without
 * one. A notification that opens the wrong screen, or nothing at all, is the
 * failure that turns "approve a spend from your pocket" back into "open the app
 * and go looking", and it is entirely decidable from the payload.
 *
 * See `src/lib/__checks__/smoke.mjs`, which imports this file directly.
 */

export type NotificationTarget =
  { kind: "approval"; id: string } | { kind: "takeover"; runId: string } | { kind: "inbox" }

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim().length > 0) return value.trim()
  }
  return null
}

/**
 * Route one notification payload.
 *
 * The API sends `{approval_id, bot_id, risk, kind}` today
 * (`apps/api/app/services/notifications.py`), so `approval` is the only kind
 * that actually arrives. `takeover` is handled anyway: a run parked on a human
 * is at least as time-sensitive as a held action and has no push of its own
 * yet, and when the API grows one the client half should already be right
 * rather than written under pressure on the day.
 *
 * Deep links (`nesqbot://approvals/<id>`) are accepted alongside the flat keys
 * because the two shapes have already diverged once, and a notification that
 * opens nothing is worse than no notification.
 *
 * **Precedence, and why.** An explicit `kind: "takeover"` wins outright. Failing
 * that, an `approval_id` wins over a `run_id`: a payload carrying both is a run
 * that parked *on an approval*, and the approval is the decision to make. A
 * bare `run_id` is the takeover case. Anything unrecognised lands on the inbox
 * rather than nowhere.
 */
export function notificationTarget(data: unknown): NotificationTarget {
  if (typeof data !== "object" || data === null) return { kind: "inbox" }
  const record = data as Record<string, unknown>

  const explicitKind = firstString(record["kind"], record["type"])
  const runId = firstString(record["run_id"], record["runId"])
  if (explicitKind === "takeover" && runId) return { kind: "takeover", runId }

  const approvalId = firstString(record["approval_id"], record["approvalId"], record["id"])
  if (approvalId) return { kind: "approval", id: approvalId }

  const url = firstString(record["url"], record["path"], record["deeplink"])
  if (url) {
    const approvalMatch = /approvals\/([^/?#]+)/.exec(url)
    if (approvalMatch) return { kind: "approval", id: approvalMatch[1] }
    const takeoverMatch = /(?:takeover|runs)\/([^/?#]+)/.exec(url)
    if (takeoverMatch) return { kind: "takeover", runId: takeoverMatch[1] }
  }

  if (runId) return { kind: "takeover", runId }
  return { kind: "inbox" }
}
