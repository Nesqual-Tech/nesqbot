/** Small display helpers shared by the screens. */

/** "just now" / "12m" / "3h" / "2d" -- compact age for list rows. */
export function relativeAge(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return ""
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return ""
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 45) return "just now"
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 7) return `${days}d ago`
  return formatDate(iso)
}

/** "14:32" for today, "12 Mar 14:32" otherwise. */
export function messageTime(iso: string | null | undefined): string {
  if (!iso) return ""
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
  const today = new Date()
  const sameDay =
    date.getDate() === today.getDate() &&
    date.getMonth() === today.getMonth() &&
    date.getFullYear() === today.getFullYear()
  if (sameDay) return time
  return `${date.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${time}`
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return ""
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })
}

export function formatUsd(amount: number | null | undefined): string {
  const value = typeof amount === "number" && Number.isFinite(amount) ? amount : 0
  if (value > 0 && value < 0.01) return "<$0.01"
  return `$${value.toFixed(2)}`
}

export function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

/** Renders an unknown JSON value as readable, indented text. */
export function prettyJson(value: unknown): string {
  if (value === null || value === undefined) return ""
  if (typeof value === "string") return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/** "send_mail" -> "Send mail" */
export function humanize(value: string): string {
  const spaced = value.replace(/[_-]+/g, " ").trim()
  if (!spaced) return value
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}
