/**
 * Microsoft Entra ID sign-in — authorization code + PKCE.
 *
 * The app opens the system browser, gets an authorization *code* back on the
 * `nesqbot://auth` deep link, and redeems it for an **access token audienced to
 * the Nesq Bot API** plus a refresh token. The access token is what authorizes
 * an API call; the ID token this flow also returns is only ever a description of
 * who signed in and is deliberately not used for authorization.
 *
 * This replaces an implicit `id_token` flow. Implicit is disabled in the
 * registration (docs/entra-setup.md), so that flow no longer works at all — and
 * should not: an ID token is audienced to *this client*, not to the API, so an
 * API accepting one cannot tell a token minted for itself from one minted for
 * any other app the same user signed into.
 *
 * Registration this expects (already provisioned — see docs/entra-setup.md):
 *  - public client `Nesq Bot`, no secret, implicit disabled
 *  - redirect URI `nesqbot://auth`
 *  - delegated `access_as_user` on the API, plus `openid`/`profile`/`offline_access`,
 *    admin-consented tenant-wide, and the client pre-authorized on the API
 */
import * as AuthSession from "expo-auth-session"
import * as WebBrowser from "expo-web-browser"
import { getEntraConfig } from "../api/client"

// Closes the in-app browser tab left over from a previous auth round-trip.
WebBrowser.maybeCompleteAuthSession()

export class EntraNotConfiguredError extends Error {
  constructor() {
    super(
      "Entra sign-in is not configured. Set EXPO_PUBLIC_ENTRA_TENANT_ID, " +
        "EXPO_PUBLIC_ENTRA_CLIENT_ID and EXPO_PUBLIC_ENTRA_SCOPE.",
    )
    this.name = "EntraNotConfiguredError"
  }
}

export class EntraCancelledError extends Error {
  constructor() {
    super("Sign-in was cancelled.")
    this.name = "EntraCancelledError"
  }
}

/** The refresh token is gone or revoked — the user has to sign in again. */
export class EntraRefreshFailedError extends Error {
  readonly cause?: unknown
  constructor(cause?: unknown) {
    super("The Microsoft session could not be renewed.")
    this.name = "EntraRefreshFailedError"
    this.cause = cause
  }
}

/** What a completed sign-in yields. Everything here goes into SecureStore. */
export interface EntraSession {
  accessToken: string
  /** Present because `offline_access` is consented; without it sessions die at ~1h. */
  refreshToken: string | null
  /** Epoch milliseconds, or null when the server did not say. */
  expiresAt: number | null
}

export function isEntraConfigured(): boolean {
  const { tenantId, clientId, scope } = getEntraConfig()
  return tenantId.length > 0 && clientId.length > 0 && scope.length > 0
}

export function getEntraRedirectUri(): string {
  const configured = getEntraConfig().redirectUri
  if (configured) return configured
  return AuthSession.makeRedirectUri({ scheme: "nesqbot", path: "auth" })
}

/**
 * The scopes to request.
 *
 * Only ONE resource may appear in a single request: `access_as_user` belongs to
 * the Nesq Bot API, so Microsoft Graph's `User.Read` cannot be bundled in here
 * even though it is consented — asking for both resources at once is rejected
 * by the endpoint. Graph scopes need their own token request if they are ever
 * wanted. `openid`/`profile`/`offline_access` are OIDC scopes and travel with
 * any resource.
 */
function scopesFor(apiScope: string): string[] {
  return [apiScope, "openid", "profile", "offline_access"]
}

async function discovery(tenantId: string): Promise<AuthSession.DiscoveryDocument> {
  return AuthSession.fetchDiscoveryAsync(`https://login.microsoftonline.com/${tenantId}/v2.0`)
}

function toSession(response: AuthSession.TokenResponse): EntraSession {
  const issuedAt = response.issuedAt ? response.issuedAt * 1000 : Date.now()
  return {
    accessToken: response.accessToken,
    refreshToken: response.refreshToken ?? null,
    expiresAt: response.expiresIn ? issuedAt + response.expiresIn * 1000 : null,
  }
}

/**
 * Runs the interactive flow and returns Entra tokens.
 *
 * PKCE is not optional here: a public client has no secret, so the code verifier
 * is the only thing binding the redeemed code to the app that asked for it. Any
 * other process that can claim the `nesqbot://` scheme sees the code on the
 * redirect and, without PKCE, could redeem it.
 */
export async function signInWithEntra(): Promise<EntraSession> {
  const { tenantId, clientId, scope } = getEntraConfig()
  if (!tenantId || !clientId || !scope) throw new EntraNotConfiguredError()

  const document = await discovery(tenantId)
  const redirectUri = getEntraRedirectUri()

  const request = new AuthSession.AuthRequest({
    clientId,
    scopes: scopesFor(scope),
    redirectUri,
    responseType: AuthSession.ResponseType.Code,
    usePKCE: true,
  })

  const result = await request.promptAsync(document)

  if (result.type === "cancel" || result.type === "dismiss") throw new EntraCancelledError()
  if (result.type === "error") throw new Error(result.error?.message ?? "Microsoft sign-in failed.")
  if (result.type !== "success") throw new Error("Microsoft sign-in did not complete.")

  const code = result.params.code
  if (!code) throw new Error("Microsoft did not return an authorization code.")
  if (!request.codeVerifier) throw new Error("The PKCE verifier went missing before redemption.")

  const tokens = await AuthSession.exchangeCodeAsync(
    {
      clientId,
      code,
      redirectUri,
      extraParams: { code_verifier: request.codeVerifier },
    },
    document,
  )

  if (!tokens.accessToken) throw new Error("Microsoft did not return an access token.")
  return toSession(tokens)
}

/**
 * Renews an expired access token without user interaction.
 *
 * Entra may or may not return a new refresh token; when it does not, the old one
 * stays valid and the caller must keep it, so `refreshToken` falls back to the
 * one passed in rather than becoming null.
 */
export async function refreshEntraSession(refreshToken: string): Promise<EntraSession> {
  const { tenantId, clientId, scope } = getEntraConfig()
  if (!tenantId || !clientId || !scope) throw new EntraNotConfiguredError()

  try {
    const document = await discovery(tenantId)
    const tokens = await AuthSession.refreshAsync(
      { clientId, refreshToken, scopes: scopesFor(scope) },
      document,
    )
    if (!tokens.accessToken) throw new Error("no access token in the refresh response")
    const session = toSession(tokens)
    return { ...session, refreshToken: session.refreshToken ?? refreshToken }
  } catch (caught) {
    throw new EntraRefreshFailedError(caught)
  }
}
