/**
 * Device preferences (API URL override, theme, notification opt-in).
 *
 * Deliberately framework-free: it is a tiny external store so both React screens
 * (via `usePreferences`) and plain modules (the API client) can read the current
 * value without an import cycle through the component tree.
 */
import { useSyncExternalStore } from "react"
import { getItem, setItem } from "./secure"

export type ThemeMode = "system" | "light" | "dark"

export interface Preferences {
  /** Overrides the compiled-in API base URL when set. Includes the `/api` prefix. */
  apiBaseUrl: string | null
  themeMode: ThemeMode
  notificationsEnabled: boolean
}

const KEY = "nesq.preferences.v1"

export const defaultPreferences: Preferences = {
  apiBaseUrl: null,
  themeMode: "system",
  notificationsEnabled: true,
}

let current: Preferences = defaultPreferences
let hydrated = false
const listeners = new Set<() => void>()

function emit(): void {
  for (const listener of listeners) listener()
}

function coerce(raw: unknown): Preferences {
  if (typeof raw !== "object" || raw === null) return defaultPreferences
  const value = raw as Partial<Record<keyof Preferences, unknown>>
  const mode = value.themeMode
  return {
    apiBaseUrl:
      typeof value.apiBaseUrl === "string" && value.apiBaseUrl.trim().length > 0 ? value.apiBaseUrl.trim() : null,
    themeMode: mode === "light" || mode === "dark" || mode === "system" ? mode : "system",
    notificationsEnabled: value.notificationsEnabled !== false,
  }
}

/** Reads persisted preferences into the in-memory snapshot. Safe to call repeatedly. */
export async function hydratePreferences(): Promise<Preferences> {
  if (hydrated) return current
  const raw = await getItem(KEY)
  if (raw) {
    try {
      current = coerce(JSON.parse(raw) as unknown)
    } catch {
      current = defaultPreferences
    }
  }
  hydrated = true
  emit()
  return current
}

export function getPreferences(): Preferences {
  return current
}

export async function updatePreferences(patch: Partial<Preferences>): Promise<Preferences> {
  current = coerce({ ...current, ...patch })
  emit()
  await setItem(KEY, JSON.stringify(current))
  return current
}

export function subscribePreferences(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function usePreferences(): Preferences {
  return useSyncExternalStore(subscribePreferences, getPreferences, getPreferences)
}
