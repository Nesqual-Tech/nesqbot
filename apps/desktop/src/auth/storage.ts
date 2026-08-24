/**
 * Where the desktop app keeps credentials.
 *
 * # The decision
 *
 * Credentials go into the **operating system credential store** — Windows
 * Credential Manager, macOS Keychain, Linux Secret Service — through the
 * `secret_get` / `secret_set` / `secret_delete` commands in the Tauri shell
 * (`src-tauri/src/secrets.rs`, which is where the mechanics and the size
 * chunking are documented). Nothing here touches `localStorage`.
 *
 * # Why, and what it is actually worth
 *
 * There is no `expo-secure-store` equivalent for a Tauri webview, so the honest
 * options were:
 *
 *  - `localStorage` — plaintext in the WebView2/WebKit profile directory, read
 *    by any process running as the user *and* by any script that gets into the
 *    page. Rejected.
 *  - `tauri-plugin-store` — a plaintext JSON file under the app data directory.
 *    Tidier than `localStorage` and no better protected. Rejected.
 *  - the OS credential store — chosen.
 *
 * What that buys, precisely: the refresh token is **not a readable file**. On
 * Windows it is DPAPI-sealed to the signed-in user account, so a copy of
 * `AppData`, a backup, a second local user, or an offline disk image does not
 * yield it. That is the realistic exfiltration path for a desktop app and it is
 * closed.
 *
 * What it does **not** buy, stated plainly: any code already running as this
 * user can ask for the same secret. That includes script execution inside our
 * own webview — an XSS in the UI, or a malicious npm dependency — because the
 * IPC commands below are reachable from the page. No client-side store fixes
 * that; a refresh token on an end-user device is protected against theft of the
 * disk, not against control of the session. Treat a compromised frontend as a
 * compromised refresh token, and revoke server-side
 * (`revokeSignInSessions`, see docs/entra-setup.md).
 *
 * # Browser fallback
 *
 * `npm run dev` in a plain browser tab has no shell, so values live in a module
 * -level `Map` for the lifetime of the tab. Deliberately not `localStorage`:
 * dev convenience is not worth writing a refresh token to disk in the clear,
 * and the cost is only that a reload signs you out.
 */
import { invokeShell, isTauri } from "../lib/tauri"

/** The Nesq Bot session JWT the API mints. Every API request carries it. */
export const SESSION_TOKEN_KEY = "nesq.auth.token"

/**
 * The Entra refresh token. Long-lived, and the reason this module exists;
 * `offline_access` is consented tenant-wide specifically so sessions survive.
 */
export const ENTRA_REFRESH_KEY = "nesq.entra.refresh"

/**
 * Cached `/me` for a fast cold start. Display data, not a credential, so it is
 * the one thing that stays in `localStorage`.
 */
export const USER_CACHE_KEY = "nesq.auth.user"

const memory = new Map<string, string>()

/**
 * Reads a credential. A store that errors is treated as empty rather than
 * fatal: the user gets a sign-in prompt, which is recoverable, instead of a
 * crash.
 */
export async function getSecret(key: string): Promise<string | null> {
  if (!isTauri) return memory.get(key) ?? null
  try {
    return (await invokeShell<string | null>("secret_get", { key })) ?? null
  } catch {
    return null
  }
}

/** Stores a credential. Returns false when the store refused it. */
export async function setSecret(key: string, value: string): Promise<boolean> {
  if (!isTauri) {
    memory.set(key, value)
    return true
  }
  try {
    await invokeShell<void>("secret_set", { key, value })
    return true
  } catch {
    // Never surface the value, and never fall back to a plaintext store.
    memory.set(key, value)
    return false
  }
}

/** Removes a credential. Absent is success — sign-out must not fail on this. */
export async function deleteSecret(key: string): Promise<void> {
  memory.delete(key)
  if (!isTauri) return
  try {
    await invokeShell<void>("secret_delete", { key })
  } catch {
    /* nothing else to do; the in-memory copy is already gone */
  }
}

/* ------------------------------------------------------- non-secret cache */

export function readUserCache(): string | null {
  try {
    return localStorage.getItem(USER_CACHE_KEY)
  } catch {
    return null
  }
}

export function writeUserCache(value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(USER_CACHE_KEY)
    else localStorage.setItem(USER_CACHE_KEY, value)
  } catch {
    /* private mode / disabled storage — the cache is optional */
  }
}
