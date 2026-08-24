# Entra ID sign-in — provisioned configuration

Set up in your own Entra tenant. Every id below is a placeholder - replace them with yours.
Everything below already exists; this file records what was made and why, and what the
apps must send.

## The two registrations

Two apps, not one. The API is a **resource server** and the clients are **public clients**.

| | Name | App (client) id |
| --- | --- | --- |
| Resource server | `Nesq Bot API` | `YOUR_API_APP_ID` |
| Public client (desktop + mobile) | `Nesq Bot` | `YOUR_CLIENT_APP_ID` |

Tenant id: `YOUR_TENANT_ID`

### Nesq Bot API

- App ID URI `api://YOUR_API_APP_ID`
- `requestedAccessTokenVersion: 2` — v2 tokens, issuer `https://login.microsoftonline.com/{tenant}/v2.0`
- Exposes one delegated scope, `access_as_user` (id `b1f1e7d2-4c3a-4f6b-9d21-8a5c7e0f3b44`)
- Pre-authorizes the client app for that scope, so no consent prompt appears on first sign-in
- Sign-in audience: this tenant only (`AzureADMyOrg`)

### Nesq Bot (client)

- Public client — no secret, and none should ever be added; a secret shipped in a desktop or
  mobile bundle is not a secret
- Redirect URIs: `nesqbot://auth` (mobile + desktop deep link), `http://localhost:1420/auth`
  (Tauri dev), `http://localhost` (loopback for the auth-code flow)
- **Implicit flow is disabled** — both ID-token and access-token issuance
- Delegated permissions, admin-consented tenant-wide:
  `access_as_user` on the API, plus Microsoft Graph `User.Read`, `openid`, `profile`,
  `offline_access`

## Why access tokens, not ID tokens

The clients were originally built to run an implicit flow and post an **ID token** to
`POST /auth/entra`. That is an accepted anti-pattern: an ID token says *who signed in* and is
audienced to the client, not to the API. Using one to authorize an API invites audience
confusion — any app the user signs into with the same client id can present a token the API
would accept.

The correct flow, which this configuration implements:

1. Client runs **auth code + PKCE** against the tenant, requesting scope
   `api://YOUR_API_APP_ID/access_as_user`
2. Entra returns an **access token** audienced to the API, plus a refresh token
   (`offline_access` is consented, so sessions survive without re-prompting)
3. Client sends `Authorization: Bearer <access token>`
4. The API validates signature against tenant JWKS, `aud` = the API app id, `iss` = the v2
   issuer for this tenant, expiry, and that `scp` contains `access_as_user`

Implicit is disabled in the registration, so the old flow now fails closed rather than
silently continuing to work — the misconfiguration cannot survive unnoticed.

## Settings

API (`apps/api`, and Key Vault in production):

```
AZURE_TENANT_ID=YOUR_TENANT_ID
AZURE_CLIENT_ID=YOUR_API_APP_ID   # the API app - the audience it accepts
AZURE_API_SCOPE=access_as_user
```

Clients (public values — these are compiled into shipped bundles and must never be secrets):

```
EXPO_PUBLIC_ENTRA_TENANT_ID=YOUR_TENANT_ID
EXPO_PUBLIC_ENTRA_CLIENT_ID=YOUR_CLIENT_APP_ID
EXPO_PUBLIC_ENTRA_SCOPE=api://YOUR_API_APP_ID/access_as_user
EXPO_PUBLIC_ENTRA_REDIRECT_URI=nesqbot://auth
```

## Not provisioned — deliberately

**The Microsoft Graph connector app.** The `microsoft_graph` connector declares `Mail.Read`,
`Mail.Send`, and `Calendars.Read`. Those are a materially larger grant than sign-in: `Mail.Send`
lets an agent send mail as a user, and admin consent for it is tenant-wide. That decision
deserves to be made deliberately rather than folded into a sign-in setup, so it is left out.

When it is wanted, it should be its own registration with its own credential in Key Vault,
bound per bot through `secret_ref` — not added to either app above. Note also that the
connector layer resolves the secret today but `_invoke_vendor` has never run against a real
tenant, so the first real Graph call is untested against live Microsoft endpoints.

## Rotation and revocation

Neither app has a client secret, so there is nothing to rotate. To cut off access, remove the
user's assignment or revoke their refresh tokens
(`az rest --method POST --url "https://graph.microsoft.com/v1.0/users/{id}/revokeSignInSessions"`).
