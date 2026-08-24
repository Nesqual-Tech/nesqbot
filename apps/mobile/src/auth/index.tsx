/**
 * Session state for the whole app.
 *
 * Three tokens, all in expo-secure-store (Keychain / EncryptedSharedPreferences):
 *
 *  - the **Nesq Bot session token** (`nesq.auth.token`), a local JWT the API
 *    mints; this is what every API request carries;
 *  - the **Entra access token** (`nesq.entra.access`), audienced to the API and
 *    only ever presented once, to `POST /auth/entra`;
 *  - the **Entra refresh token** (`nesq.entra.refresh`), which exists because
 *    `offline_access` is consented tenant-wide specifically so that a session
 *    outlives the ~1h access-token lifetime.
 *
 * A 401 therefore means "renew", not "sign out". Only a refresh that Entra
 * itself rejects ends the session — anything else (offline, API restart, a
 * momentarily expired access token) is recoverable and must not throw the user
 * back to the login screen.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import type { User } from "@nesqbot/protocol"
import { api } from "../api/endpoints"
import { ApiError, setAccessToken, setUnauthorizedHandler } from "../api/client"
import { getItem, removeItem, setItem } from "../storage/secure"
import {
  EntraRefreshFailedError,
  refreshEntraSession,
  signInWithEntra as acquireEntraSession,
  type EntraSession,
} from "./entra"

const TOKEN_KEY = "nesq.auth.token"
const USER_KEY = "nesq.auth.user"
const ENTRA_ACCESS_KEY = "nesq.entra.access"
const ENTRA_REFRESH_KEY = "nesq.entra.refresh"
const ENTRA_EXPIRES_KEY = "nesq.entra.expires"

export type AuthStatus = "loading" | "authenticated" | "unauthenticated"

export interface AuthContextValue {
  status: AuthStatus
  user: User | null
  token: string | null
  /** Set when the last sign-in attempt failed. */
  error: unknown
  signInDev: () => Promise<void>
  signInWithEntra: () => Promise<void>
  signOut: () => Promise<void>
  /** Re-reads `/me`; used after an API URL change. */
  revalidate: () => Promise<void>
  clearError: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const [status, setStatus] = useState<AuthStatus>("loading")
  const [user, setUser] = useState<User | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const mounted = useRef(true)
  /** One renewal at a time: a burst of 401s must not fan out into N refreshes. */
  const renewal = useRef<Promise<boolean> | null>(null)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const applySession = useCallback(async (nextToken: string, nextUser: User) => {
    setAccessToken(nextToken)
    await setItem(TOKEN_KEY, nextToken)
    await setItem(USER_KEY, JSON.stringify(nextUser))
    if (!mounted.current) return
    setToken(nextToken)
    setUser(nextUser)
    setError(null)
    setStatus("authenticated")
  }, [])

  const storeEntraSession = useCallback(async (session: EntraSession) => {
    await setItem(ENTRA_ACCESS_KEY, session.accessToken)
    if (session.refreshToken) await setItem(ENTRA_REFRESH_KEY, session.refreshToken)
    await setItem(ENTRA_EXPIRES_KEY, session.expiresAt ? String(session.expiresAt) : "")
  }, [])

  const clearSession = useCallback(async () => {
    setAccessToken(null)
    await removeItem(TOKEN_KEY)
    await removeItem(USER_KEY)
    await removeItem(ENTRA_ACCESS_KEY)
    await removeItem(ENTRA_REFRESH_KEY)
    await removeItem(ENTRA_EXPIRES_KEY)
    if (!mounted.current) return
    setToken(null)
    setUser(null)
    setStatus("unauthenticated")
  }, [])

  /** Swaps an Entra access token for a Nesq Bot session token. */
  const exchange = useCallback(
    async (session: EntraSession) => {
      await storeEntraSession(session)
      const result = await api.auth.entra(session.accessToken)
      await applySession(result.access_token, result.user)
    },
    [applySession, storeEntraSession],
  )

  /**
   * Tries to get a working session back without user interaction.
   *
   * Two attempts, cheapest first. A 401 from *our* API says the local session
   * token died; it says nothing about the Entra access token, which is typically
   * still inside its hour — so that is tried first and usually costs one request.
   * Only if it is gone or refused do we spend a round trip on the refresh token.
   *
   * Returns false only when there is nothing left to try. A failure that is not
   * Entra's verdict (API down, offline) leaves the credentials in place and
   * reports success, because signing the user out over a flaky network is the
   * one outcome `offline_access` was consented to prevent.
   */
  const renew = useCallback(async (): Promise<boolean> => {
    const stored = await getItem(ENTRA_ACCESS_KEY)
    const expiresRaw = await getItem(ENTRA_EXPIRES_KEY)
    const expiresAt = expiresRaw ? Number(expiresRaw) : null
    const stillFresh = !expiresAt || Number.isNaN(expiresAt) || expiresAt - Date.now() > 60_000

    if (stored && stillFresh) {
      try {
        await exchange({ accessToken: stored, refreshToken: null, expiresAt })
        return true
      } catch (caught) {
        // Anything other than Entra rejecting the token means "try again later",
        // not "sign out".
        if (caught instanceof ApiError && caught.status !== 401 && caught.status !== 403) {
          return true
        }
      }
    }

    const refreshToken = await getItem(ENTRA_REFRESH_KEY)
    if (!refreshToken) return false
    try {
      await exchange(await refreshEntraSession(refreshToken))
      return true
    } catch (caught) {
      if (caught instanceof EntraRefreshFailedError) return false
      if (caught instanceof ApiError && caught.status !== 401 && caught.status !== 403) {
        return true
      }
      return false
    }
  }, [exchange])

  /** Coalesces concurrent renewal attempts onto one in-flight promise. */
  const renewOnce = useCallback(async (): Promise<boolean> => {
    if (!renewal.current) {
      renewal.current = renew().finally(() => {
        renewal.current = null
      })
    }
    return renewal.current
  }, [renew])

  /** Restores a stored session on cold start. */
  const restore = useCallback(async () => {
    const storedToken = await getItem(TOKEN_KEY)
    if (!storedToken) {
      if (mounted.current) setStatus("unauthenticated")
      return
    }

    setAccessToken(storedToken)
    const cachedRaw = await getItem(USER_KEY)
    let cachedUser: User | null = null
    if (cachedRaw) {
      try {
        cachedUser = JSON.parse(cachedRaw) as User
      } catch {
        cachedUser = null
      }
    }
    if (mounted.current) {
      setToken(storedToken)
      setUser(cachedUser)
    }

    try {
      const fresh = await api.auth.me()
      if (!mounted.current) return
      setUser(fresh)
      void setItem(USER_KEY, JSON.stringify(fresh))
      setStatus("authenticated")
    } catch (caught) {
      // Only a definitive rejection signs the user out; being offline must not.
      if (caught instanceof ApiError && (caught.status === 401 || caught.status === 403)) {
        if (await renewOnce()) return
        await clearSession()
        return
      }
      if (mounted.current) setStatus("authenticated")
    }
  }, [clearSession, renewOnce])

  useEffect(() => {
    void restore()
  }, [restore])

  // A 401 from any request means the local session token died. Try the Entra
  // refresh token first — that is what `offline_access` was consented for — and
  // only sign out when Entra itself will not renew.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (!mounted.current) return
      void (async () => {
        if (await renewOnce()) return
        if (mounted.current) await clearSession()
      })()
    })
    return () => setUnauthorizedHandler(null)
  }, [clearSession, renewOnce])

  const signInDev = useCallback(async () => {
    setError(null)
    try {
      const result = await api.auth.devLogin()
      await applySession(result.access_token, result.user)
    } catch (caught) {
      if (mounted.current) setError(caught)
      throw caught
    }
  }, [applySession])

  const signInWithEntra = useCallback(async () => {
    setError(null)
    try {
      await exchange(await acquireEntraSession())
    } catch (caught) {
      if (mounted.current) setError(caught)
      throw caught
    }
  }, [exchange])

  const signOut = useCallback(async () => {
    await clearSession()
  }, [clearSession])

  const revalidate = useCallback(async () => {
    setStatus("loading")
    await restore()
  }, [restore])

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      token,
      error,
      signInDev,
      signInWithEntra,
      signOut,
      revalidate,
      clearError: () => setError(null),
    }),
    [status, user, token, error, signInDev, signInWithEntra, signOut, revalidate],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>")
  return value
}

export {
  EntraCancelledError,
  EntraNotConfiguredError,
  EntraRefreshFailedError,
  getEntraRedirectUri,
  isEntraConfigured,
  refreshEntraSession,
} from "./entra"
export type { EntraSession } from "./entra"
