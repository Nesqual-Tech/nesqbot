/**
 * Microsoft Entra ID sign-in for the desktop app — authorization code + PKCE.
 *
 * The shape mirrors `apps/mobile/src/auth/entra.ts`; only the transport differs,
 * because there is no `expo-auth-session` here. The steps are:
 *
 *  1. mint a PKCE verifier and its S256 challenge, and a `state` nonce;
 *  2. ask the shell to open the system browser at the authorize endpoint with
 *     `redirect_uri=nesqbot://auth`;
 *  3. take the `nesqbot://auth?code=…&state=…` deep link back;
 *  4. redeem the code **from the Rust side** for an access token audienced to
 *     the Nesq Bot API, plus a refresh token.
 *
 * Step 4 is the one that needed native support: a webview `fetch` carries an
 * `Origin` header and Entra refuses cross-origin redemption for native
 * public-client redirect URIs (`AADSTS9002326`). It is therefore done by the
 * `redeem_entra_code` / `refresh_entra_token` commands in
 * `src-tauri/src/entra.rs`, whose HTTP client sends no `Origin` at all.
 *
 * `tauri-plugin-http` was tried for this and does not work: it runs in the
 * shell process but forwards the webview's origin anyway, and an empty `Origin`
 * header — the documented way to suppress it — did not change the outcome. The
 * plugin has been removed; do not reintroduce it here.
 *
 * The ID token this flow also returns is deliberately unused: it is audienced
 * to *this client*, not to the API, so authorizing with one invites audience
 * confusion. Only the access token is ever presented to `POST /auth/entra`.
 *
 * Nothing in this module logs a code, a verifier, or a token — including on
 * error paths. Entra's `error` / `error_description` are safe to surface (they
 * carry an AADSTS code and a correlation id) and are all that ever escapes.
 */
import { invokeShell } from "../lib/tauri"

/* ------------------------------------------------------------------ config */

export interface EntraConfig {
  /** Directory (tenant) id — the authority the app signs in against. */
  tenantId: string
  /** The **public client** app id (`Nesq Bot`), not the API's. */
  clientId: string
  /** Full scope URI: `api://<api app id>/access_as_user`. */
  scope: string
  /** Must be a redirect URI registered on the client app. */
  redirectUri: string
}

/**
 * Every value here is public: it ships inside the installer, so none of it may
 * ever be a secret, and the app has no client secret at all (docs/entra-setup.md
 * — a secret in a shipped desktop bundle is not a secret).
 *
 * There are no built-in defaults for the tenant or the app registrations. This
 * is a source-available build and the registration belongs to whoever deploys
 * it: supply `VITE_ENTRA_TENANT_ID`, `VITE_ENTRA_CLIENT_ID` and
 * `VITE_ENTRA_SCOPE` at build time (see docs/entra-setup.md).
 *
 * Leaving them unset is fail-closed, not fail-open. `isEntraConfigured()`
 * returns false, the sign-in button is not offered at all, and the app falls
 * back to the local development login — which itself only works while the API
 * is running with `NESQ_ENV=development`.
 */
const DEFAULTS: EntraConfig = {
  tenantId: "",
  clientId: "",
  scope: "",
  redirectUri: "nesqbot://auth",
}

export function getEntraConfig(): EntraConfig {
  const env = import.meta.env
  return {
    tenantId: env.VITE_ENTRA_TENANT_ID || DEFAULTS.tenantId,
    clientId: env.VITE_ENTRA_CLIENT_ID || DEFAULTS.clientId,
    scope: env.VITE_ENTRA_SCOPE || DEFAULTS.scope,
    redirectUri: env.VITE_ENTRA_REDIRECT_URI || DEFAULTS.redirectUri,
  }
}

export function isEntraConfigured(): boolean {
  const { tenantId, clientId, scope, redirectUri } = getEntraConfig()
  return Boolean(tenantId && clientId && scope && redirectUri)
}

/**
 * The scopes to request.
 *
 * Only ONE resource may appear in a single request: `access_as_user` belongs to
 * the Nesq Bot API, so Microsoft Graph's `User.Read` cannot be bundled in here
 * even though it is consented — asking for two resources at once is rejected.
 * `openid`/`profile`/`offline_access` are OIDC scopes and travel with any
 * resource. `offline_access` is what produces the refresh token.
 */
function scopesFor(apiScope: string): string {
  return [apiScope, "openid", "profile", "offline_access"].join(" ")
}

const authorizeUrl = (tenantId: string) =>
  `https://login.microsoftonline.com/${encodeURIComponent(tenantId)}/oauth2/v2.0/authorize`
// There is no `tokenUrl` here on purpose: the token endpoint is built inside
// the shell (`src-tauri/src/entra.rs`), which pins the authority, so the
// frontend cannot point token redemption at a host of its choosing.

/* ------------------------------------------------------------------ errors */

export class EntraNotConfiguredError extends Error {
  constructor() {
    super("Microsoft sign-in is not configured for this build.")
    this.name = "EntraNotConfiguredError"
  }
}

export class EntraCancelledError extends Error {
  constructor() {
    super("Sign-in was cancelled.")
    this.name = "EntraCancelledError"
  }
}

/** Entra refused the request. `code` is the OAuth error, e.g. `invalid_grant`. */
export class EntraResponseError extends Error {
  readonly code: string
  constructor(code: string, description: string) {
    super(description || code || "Microsoft sign-in failed.")
    this.name = "EntraResponseError"
    this.code = code
  }
}

/** The refresh token is gone or revoked — the user has to sign in again. */
export class EntraRefreshFailedError extends Error {
  readonly reason?: unknown
  constructor(reason?: unknown) {
    super("The Microsoft session could not be renewed.")
    this.name = "EntraRefreshFailedError"
    this.reason = reason
  }
}

/* -------------------------------------------------------------------- PKCE */

function base64Url(bytes: Uint8Array): string {
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

function randomToken(byteLength: number): string {
  const bytes = new Uint8Array(byteLength)
  crypto.getRandomValues(bytes)
  return base64Url(bytes)
}

/**
 * PKCE is not optional for a public client: with no secret, the verifier is the
 * only thing binding the redeemed code to the process that asked for it. Any
 * other local program can register the `nesqbot://` scheme and see the code on
 * the redirect; without PKCE it could redeem it.
 */
async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))
  return base64Url(new Uint8Array(digest))
}

/* ------------------------------------------------------------------ tokens */

/** What a completed sign-in yields. */
export interface EntraSession {
  /** Audienced to the Nesq Bot API; presented once, to `POST /auth/entra`. */
  accessToken: string
  /** Present because `offline_access` is consented; null if Entra omitted it. */
  refreshToken: string | null
  /** Epoch milliseconds, or null when the server did not say. */
  expiresAt: number | null
}

/** What `redeem_entra_code` / `refresh_entra_token` resolve with. */
interface ShellTokenSuccess {
  accessToken: string
  refreshToken: string | null
  expiresIn: number | null
}

/** What they reject with: `TokenError` in `src-tauri/src/entra.rs`. */
interface ShellTokenError {
  code: string
  description: string
}

function isShellTokenError(value: unknown): value is ShellTokenError {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as ShellTokenError).code === "string" &&
    typeof (value as ShellTokenError).description === "string"
  )
}

/**
 * Redeems a grant through the shell.
 *
 * The POST itself happens in `src-tauri/src/entra.rs`, on a `reqwest` client
 * that sends **no `Origin` header** — which is the whole reason this is not a
 * `fetch`. Entra permits cross-origin redemption only for the Single-Page
 * Application client type and answers `AADSTS9002326` to anything carrying an
 * origin, whatever its value; this app is a native public client, so the header
 * has to be absent rather than empty. Do not move this back into the webview,
 * and do not route it through an HTTP plugin: both were shipped and both
 * failed with exactly that code.
 *
 * The shell's error shape maps 1:1 onto `EntraResponseError`, including the
 * synthetic `network_error` code that `refreshEntraSession` below depends on to
 * tell "Entra refused" apart from "we could not ask".
 */
async function postToken(
  command: "redeem_entra_code" | "refresh_entra_token",
  args: Record<string, string>,
): Promise<EntraSession> {
  let payload: ShellTokenSuccess
  try {
    payload = await invokeShell<ShellTokenSuccess>(command, args)
  } catch (caught) {
    // Entra's own refusal, carrying an AADSTS code and a correlation id.
    if (isShellTokenError(caught)) throw new EntraResponseError(caught.code, caught.description)
    // Anything else is the IPC boundary failing, not a verdict on the session.
    // `args` holds the code / verifier / refresh token and is never echoed.
    throw new EntraResponseError(
      "network_error",
      caught instanceof Error ? caught.message : "Could not reach Microsoft.",
    )
  }

  if (!payload?.accessToken) {
    throw new EntraResponseError("no_access_token", "Microsoft did not return an access token.")
  }

  return {
    accessToken: payload.accessToken,
    refreshToken: payload.refreshToken ?? null,
    expiresAt: payload.expiresIn ? Date.now() + payload.expiresIn * 1000 : null,
  }
}

/* --------------------------------------------------------- pending request */

interface PendingSignIn {
  state: string
  verifier: string
  resolve: (session: EntraSession) => void
  reject: (error: unknown) => void
  timer: ReturnType<typeof setTimeout>
}

/**
 * One interactive sign-in at a time. It lives at module scope because the
 * redirect arrives on a shell event, not as the return value of anything the
 * caller is awaiting.
 */
let pending: PendingSignIn | null = null

/** How long to wait for the browser round trip before giving up. */
const REDIRECT_TIMEOUT_MS = 5 * 60_000

function settle(): PendingSignIn | null {
  const current = pending
  if (current) {
    clearTimeout(current.timer)
    pending = null
  }
  return current
}

/** Abandons an in-flight sign-in — used when the user starts another one. */
export function cancelPendingSignIn(): void {
  settle()?.reject(new EntraCancelledError())
}

/**
 * Consumes a `nesqbot://auth?…` deep link.
 *
 * Returns true when the redirect belonged to the sign-in this app started, so
 * the caller can tell a real redirect from a stray one and stay silent either
 * way. **`state` is checked before anything else is read.** A custom scheme is
 * an OS-level entry point: any local process can invoke `nesqbot://auth?code=…`
 * with a code it obtained itself, so an unvalidated redirect is an injection
 * point. A mismatch is ignored rather than treated as a failure — the genuine
 * redirect may still be on its way.
 *
 * The caller must never log or display `params`; it carries the one-time code.
 */
export function completeEntraRedirect(params: Record<string, string>): boolean {
  const current = pending
  if (!current) return false
  if (params.state !== current.state) return false

  if (params.error) {
    settle()
    const code = params.error
    if (code === "access_denied") current.reject(new EntraCancelledError())
    else current.reject(new EntraResponseError(code, params.error_description ?? ""))
    return true
  }

  const code = params.code
  if (!code) return false

  settle()
  const { tenantId, clientId, scope, redirectUri } = getEntraConfig()
  void postToken("redeem_entra_code", {
    tenantId,
    clientId,
    code,
    redirectUri,
    codeVerifier: current.verifier,
    scope: scopesFor(scope),
  }).then(current.resolve, current.reject)

  return true
}

/**
 * Runs the interactive flow: opens the browser, then resolves once the redirect
 * comes back and the code has been redeemed.
 */
export async function signInWithEntra(): Promise<EntraSession> {
  const { tenantId, clientId, scope, redirectUri } = getEntraConfig()
  if (!tenantId || !clientId || !scope || !redirectUri) throw new EntraNotConfiguredError()

  cancelPendingSignIn()

  // 32 bytes each: 43 base64url characters, inside RFC 7636's 43–128 range.
  const verifier = randomToken(32)
  const state = randomToken(32)
  const challenge = await challengeFor(verifier)

  const query = new URLSearchParams({
    client_id: clientId,
    response_type: "code",
    redirect_uri: redirectUri,
    response_mode: "query",
    scope: scopesFor(scope),
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
    // Without this a signed-in browser silently reuses its account, which is
    // the wrong default on a shared machine.
    prompt: "select_account",
  })

  const settled = new Promise<EntraSession>((resolve, reject) => {
    pending = {
      state,
      verifier,
      resolve,
      reject,
      timer: setTimeout(() => {
        settle()
        reject(new EntraCancelledError())
      }, REDIRECT_TIMEOUT_MS),
    }
  })

  try {
    // The URL carries the challenge (a hash) and the state nonce, never the
    // verifier — the whole point of PKCE is that this half is safe in the open.
    await invokeShell<void>("open_sign_in_url", { url: `${authorizeUrl(tenantId)}?${query.toString()}` })
  } catch (caught) {
    settle()
    throw caught
  }

  return settled
}

/**
 * Renews an expired access token without user interaction.
 *
 * Entra may or may not return a new refresh token; when it does not the old one
 * stays valid, so `refreshToken` falls back to the one passed in rather than
 * becoming null.
 */
export async function refreshEntraSession(refreshToken: string): Promise<EntraSession> {
  const { tenantId, clientId, scope } = getEntraConfig()
  if (!tenantId || !clientId || !scope) throw new EntraNotConfiguredError()

  try {
    const session = await postToken("refresh_entra_token", {
      tenantId,
      clientId,
      refreshToken,
      scope: scopesFor(scope),
    })
    return { ...session, refreshToken: session.refreshToken ?? refreshToken }
  } catch (caught) {
    // A network failure is not Entra's verdict, and the caller must be able to
    // tell the two apart: only a refusal may end the session.
    if (caught instanceof EntraResponseError && caught.code === "network_error") throw caught
    throw new EntraRefreshFailedError(caught)
  }
}
