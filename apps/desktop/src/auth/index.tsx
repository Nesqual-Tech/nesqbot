/**
 * Session state for the desktop app.
 *
 * Mirrors `apps/mobile/src/auth/index.tsx`, because the rule it encodes is the
 * same on both lanes: **a 401 means "renew", not "sign out"**. `offline_access`
 * is consented tenant-wide specifically so a session outlives the ~1h
 * access-token lifetime, and only a refresh that Entra itself refuses ends it.
 * Anything else — the API restarting, being offline, a momentarily expired
 * token — is recoverable and must not throw the user back to a login screen.
 *
 * Three credentials, and they are stored differently on purpose:
 *
 *  - the **Nesq Bot session token**, a local JWT the API mints, in the OS
 *    credential store (`storage.ts`); this is what every API request carries;
 *  - the **Entra refresh token**, also in the credential store — it is the
 *    long-lived one and the reason that store was chosen;
 *  - the **Entra access token**, in memory only. It lives ~1h and is presented
 *    exactly once, to `POST /auth/entra`. Keeping it out of the credential
 *    store both narrows what is at rest and sidesteps Windows' 2560-byte
 *    credential-blob limit for the largest of the three values. The cost is
 *    that a cold start goes straight to the refresh token instead of trying a
 *    cached access token first; within a session the cheap path still applies.
 *
 * Sign-in is additive: the app has always worked against a dev API through the
 * `X-Nesq-Dev` header, and it still does when nobody has signed in. Nothing
 * here gates the UI.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { ApiError, setToken as setClientToken, setUnauthorizedHandler } from "../api/client"
import { devLogin, entraLogin, getMe } from "../api/endpoints"
import type { User } from "../types"
import {
  ENTRA_REFRESH_KEY,
  SESSION_TOKEN_KEY,
  deleteSecret,
  getSecret,
  readUserCache,
  setSecret,
  writeUserCache,
} from "./storage"
import {
  EntraCancelledError,
  EntraRefreshFailedError,
  completeEntraRedirect,
  isEntraConfigured,
  refreshEntraSession,
  signInWithEntra as acquireEntraSession,
  type EntraSession,
} from "./entra"

export type AuthStatus = "loading" | "authenticated" | "unauthenticated"

export interface AuthContextValue {
  status: AuthStatus
  user: User | null
  token: string | null
  /** True while an interactive Microsoft sign-in is waiting on the browser. */
  signingIn: boolean
  /** Set when the last sign-in attempt failed. */
  error: unknown
  /** Whether this build has an Entra registration to sign in against. */
  entraAvailable: boolean
  signInDev: () => Promise<void>
  signInWithEntra: () => Promise<void>
  signOut: () => Promise<void>
  clearError: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading")
  const [user, setUser] = useState<User | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [signingIn, setSigningIn] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const mounted = useRef(true)
  /** The Entra access token: memory only, never written to disk. */
  const entraAccess = useRef<{ token: string; expiresAt: number | null } | null>(null)
  /** One renewal at a time: a burst of 401s must not fan out into N refreshes. */
  const renewal = useRef<Promise<boolean> | null>(null)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const applySession = useCallback(async (nextToken: string, nextUser: User) => {
    setClientToken(nextToken)
    await setSecret(SESSION_TOKEN_KEY, nextToken)
    writeUserCache(JSON.stringify(nextUser))
    if (!mounted.current) return
    setToken(nextToken)
    setUser(nextUser)
    setError(null)
    setStatus("authenticated")
  }, [])

  const clearSession = useCallback(async () => {
    setClientToken(null)
    entraAccess.current = null
    await deleteSecret(SESSION_TOKEN_KEY)
    await deleteSecret(ENTRA_REFRESH_KEY)
    writeUserCache(null)
    if (!mounted.current) return
    setToken(null)
    setUser(null)
    setStatus("unauthenticated")
  }, [])

  /** Swaps an Entra access token for a Nesq Bot session token. */
  const exchange = useCallback(
    async (session: EntraSession) => {
      entraAccess.current = { token: session.accessToken, expiresAt: session.expiresAt }
      if (session.refreshToken) await setSecret(ENTRA_REFRESH_KEY, session.refreshToken)
      const result = await entraLogin(session.accessToken)
      await applySession(result.access_token, result.user)
    },
    [applySession],
  )

  /**
   * Tries to get a working session back without user interaction.
   *
   * Two attempts, cheapest first. A 401 from *our* API says the local session
   * token died; it says nothing about the Entra access token, which is usually
   * still inside its hour — so that is tried first and costs one request. Only
   * if it is missing, stale or refused do we spend a round trip on Entra.
   *
   * Returns false only when there is nothing left to try. A failure that is not
   * Entra's verdict (API down, offline) leaves the credentials in place and
   * reports success, because signing the user out over a flaky network is the
   * one outcome `offline_access` was consented to prevent.
   */
  const renew = useCallback(async (): Promise<boolean> => {
    const cached = entraAccess.current
    const stillFresh = cached && (cached.expiresAt === null || cached.expiresAt - Date.now() > 60_000)

    if (cached && stillFresh) {
      try {
        await exchange({ accessToken: cached.token, refreshToken: null, expiresAt: cached.expiresAt })
        return true
      } catch (caught) {
        // Anything other than the API rejecting the token means "try again
        // later", not "sign out".
        if (caught instanceof ApiError && !caught.isAuth) return true
      }
    }

    const refreshToken = await getSecret(ENTRA_REFRESH_KEY)
    if (!refreshToken) return false

    try {
      await exchange(await refreshEntraSession(refreshToken))
      return true
    } catch (caught) {
      if (caught instanceof EntraRefreshFailedError) return false
      if (caught instanceof ApiError && !caught.isAuth) return true
      // A network error from the refresh surfaces as EntraResponseError
      // ("network_error"); keep the credentials and try again next time.
      return true
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
    const storedToken = await getSecret(SESSION_TOKEN_KEY)
    if (!storedToken) {
      if (mounted.current) setStatus("unauthenticated")
      return
    }

    setClientToken(storedToken)
    const cachedRaw = readUserCache()
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
      const fresh = await getMe()
      if (!mounted.current) return
      setUser(fresh)
      writeUserCache(JSON.stringify(fresh))
      setStatus("authenticated")
    } catch (caught) {
      // Only a definitive rejection signs the user out; being offline must not.
      if (caught instanceof ApiError && caught.isAuth) {
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

  // A 401 from any request means the local session token died. Try renewal
  // first — that is what `offline_access` was consented for — and only sign out
  // when Entra itself will not renew.
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
      const result = await devLogin()
      await applySession(result.access_token, result.user)
    } catch (caught) {
      if (mounted.current) setError(caught)
      throw caught
    }
  }, [applySession])

  const signIn = useCallback(async () => {
    setError(null)
    setSigningIn(true)
    try {
      await exchange(await acquireEntraSession())
    } catch (caught) {
      // A cancelled sign-in is a normal outcome, not an error to display.
      if (mounted.current && !(caught instanceof EntraCancelledError)) setError(caught)
      throw caught
    } finally {
      if (mounted.current) setSigningIn(false)
    }
  }, [exchange])

  const signOut = useCallback(async () => {
    // Local only. Revoking at Microsoft is `revokeSignInSessions`, an admin
    // action — see docs/entra-setup.md.
    await clearSession()
  }, [clearSession])

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      token,
      signingIn,
      error,
      entraAvailable: isEntraConfigured(),
      signInDev,
      signInWithEntra: signIn,
      signOut,
      clearError: () => setError(null),
    }),
    [status, user, token, signingIn, error, signInDev, signIn, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>")
  return value
}

export { completeEntraRedirect, isEntraConfigured }
export {
  EntraCancelledError,
  EntraNotConfiguredError,
  EntraRefreshFailedError,
  EntraResponseError,
  getEntraConfig,
} from "./entra"
export type { EntraSession } from "./entra"
