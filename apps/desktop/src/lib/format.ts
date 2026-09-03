/** Tiny formatting/utility helpers. No dependencies. */

const usdFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const usdPreciseFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
})

export function usd(value: number | null | undefined, precise = false): string {
  const n = typeof value === "number" && Number.isFinite(value) ? value : 0
  return precise ? usdPreciseFormatter.format(n) : usdFormatter.format(n)
}

/**
 * Money at the precision the number actually needs.
 *
 * The usage panel asked for `precise` everywhere, so a day's spend rendered as
 * `$34.2900 of $70.00` — four decimal places against two, in the same
 * sentence. Nobody reads the last two digits of a thirty-four dollar total,
 * and a reader who notices them reads them as a bug, which for a product being
 * sold on its handling of money is an expensive impression to make.
 *
 * Four places exist for a real reason: a single `nano` tier call costs a
 * fraction of a cent, and rounding it to `$0.00` would say the work was free.
 * So the precision follows the magnitude — sub-cent amounts keep their digits,
 * everything else gets the two a currency has.
 */
export function usdSmart(value: number | null | undefined): string {
  const n = typeof value === "number" && Number.isFinite(value) ? value : 0
  return n !== 0 && Math.abs(n) < 0.01 ? usdPreciseFormatter.format(n) : usdFormatter.format(n)
}

/** `1 step` / `3 steps`. */
export function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`
}

const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

/**
 * A five-field cron expression, in English.
 *
 * Routines are the feature the product describes as "taught from real desktop
 * demonstrations" — the pitch is explicitly that a non-engineer can build one.
 * The panel then labelled every schedule with the raw expression: `0 7 * * 1-5`.
 * Someone who can read that did not need the routine builder, and someone who
 * needed the routine builder cannot read that.
 *
 * This covers the shapes a scheduler UI actually produces — a fixed time, on
 * some set of days, or every N minutes/hours. Anything more exotic (step
 * syntax in several fields, lists of hours, `@`-shorthands) is left as the raw
 * expression rather than mistranslated: a wrong sentence about when something
 * runs is worse than an honest one nobody reads. The input stays authoritative
 * either way — this only ever labels it.
 */
export function describeCron(expression?: string | null): string | null {
  const raw = expression?.trim()
  if (!raw) return null
  const parts = raw.split(/\s+/)
  if (parts.length !== 5) return null
  const [minute, hour, dom, month, dow] = parts

  // Only day-of-week and time are interpreted; a day-of-month or month
  // restriction changes the meaning and is not worth guessing at.
  if (dom !== "*" || month !== "*") return null

  const num = (v: string): number | null => (/^\d{1,2}$/.test(v) ? Number(v) : null)

  const when = (): string | null => {
    // Every N minutes.
    if (hour === "*" && /^\*\/\d{1,2}$/.test(minute)) return `every ${plural(Number(minute.slice(2)), "minute")}`
    // Every N hours, on the hour.
    if (/^\*\/\d{1,2}$/.test(hour) && num(minute) === 0) return `every ${plural(Number(hour.slice(2)), "hour")}`
    // Hourly.
    if (hour === "*" && num(minute) !== null) return `hourly at :${String(num(minute)).padStart(2, "0")}`
    // A fixed clock time.
    const h = num(hour)
    const m = num(minute)
    if (h !== null && m !== null && h < 24 && m < 60) {
      return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`
    }
    return null
  }

  const days = (): string | null => {
    if (dow === "*") return null
    if (dow === "1-5") return "weekdays"
    if (dow === "0,6" || dow === "6,0" || dow === "6-7" || dow === "0" || dow === "6") {
      return dow.length === 1 ? `${DAY_NAMES[Number(dow) % 7]}s` : "weekends"
    }
    const single = num(dow)
    if (single !== null && single <= 7) return `${DAY_NAMES[single % 7]}s`
    return null
  }

  const time = when()
  if (!time) return null
  const day = days()
  if (dow !== "*" && !day) return null

  // "every 15 minutes" reads as a frequency; "07:00" reads as a time.
  const frequency = time.startsWith("every") || time.startsWith("hourly")
  if (!day) return frequency ? time[0].toUpperCase() + time.slice(1) : `Daily at ${time}`
  return frequency ? `${time[0].toUpperCase() + time.slice(1)}, ${day}` : `${day[0].toUpperCase() + day.slice(1)} at ${time}`
}

export function pct(value: number, total: number): number {
  if (!total || !Number.isFinite(total)) return 0
  return Math.max(0, Math.min(999, (value / total) * 100))
}

export function compactNumber(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0"
  if (Math.abs(value) < 1000) return String(value)
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value)
}

/** "just now" / "4m ago" / "22 Aug 09:14" */
export function relativeTime(iso?: string | number | null): string {
  if (iso === undefined || iso === null || iso === "") return ""
  const date = typeof iso === "number" ? new Date(iso) : new Date(iso)
  const time = date.getTime()
  if (Number.isNaN(time)) return ""
  const diff = Date.now() - time
  if (diff < 45_000) return "just now"
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  })
}

/**
 * "12:25" / "Wed" / "22 Aug" — the stamp at the end of a conversation row.
 *
 * Deliberately not `relativeTime`. A list of thirty conversations reading
 * "4m ago / 2h ago / 19h ago / 3d ago" is a column of arithmetic nobody does;
 * every messenger settles on this ladder instead, because "Wed" is a fact and
 * "3d ago" is a subtraction. Same precision, less work to read.
 */
export function conversationTime(iso?: string | null): string {
  if (!iso) return ""
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  const now = new Date()
  const sameDay =
    date.getDate() === now.getDate() &&
    date.getMonth() === now.getMonth() &&
    date.getFullYear() === now.getFullYear()
  if (sameDay) return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
  // Inside the last week a weekday name is unambiguous; past that it is not,
  // so it becomes a date.
  if (Date.now() - date.getTime() < 6 * 86_400_000) {
    return date.toLocaleDateString(undefined, { weekday: "short" })
  }
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" })
}

export function clockTime(iso?: string | null): string {
  if (!iso) return ""
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
}

export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return "??"
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

/** Class-name joiner. */
export function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ")
}

export function truncate(text: string, max = 120): string {
  if (text.length <= max) return text
  return `${text.slice(0, max - 1)}…`
}

let counter = 0
/** Stable-enough client id (crypto.randomUUID when available). */
export function uid(prefix = "id"): string {
  counter += 1
  const rand =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  return `${prefix}_${counter}_${rand}`
}

/** Very small cron sanity check — 5 or 6 space-separated fields. */
export function isPlausibleCron(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return true // empty means "no schedule"
  const fields = trimmed.split(/\s+/)
  if (fields.length !== 5 && fields.length !== 6) return false
  return fields.every((f) => /^[\d*/,\-?A-Za-z]+$/.test(f))
}
