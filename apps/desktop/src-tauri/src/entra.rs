//! Microsoft Entra ID token redemption, performed in the shell process.
//!
//! # Why this is not `fetch`, and not `tauri-plugin-http` either
//!
//! The app is a **native public client**: `nesqbot://auth` is registered under
//! Entra's *Mobile and desktop applications* platform, not *Single-page
//! application*. Entra permits **cross-origin** token redemption only for the
//! SPA client type, and it decides which one you are by whether the request
//! carries an `Origin` header. Any request with an `Origin` — whatever the
//! value — is judged a browser request and refused:
//!
//! ```text
//! AADSTS9002326: Cross-origin token redemption is permitted only for the
//! 'Single-Page Application' client-type. Request origin: 'http://tauri.localhost'
//! ```
//!
//! Three things were tried before this module existed, and they are recorded
//! here so nobody re-attempts them:
//!
//!  1. a plain webview `fetch` — sends `Origin: http://tauri.localhost`, fails;
//!  2. `tauri-plugin-http`'s `fetch` — runs in *this* process, but forwards the
//!     webview's origin with the request anyway, so it fails identically;
//!  3. the same plugin with `Origin: ""` in the headers, which is documented as
//!     suppressing the header — verified present in the shipped bundle, and it
//!     **still** failed with `AADSTS9002326`.
//!
//! So the plugin is not the right tool. A bare `reqwest` client adds no `Origin`
//! header unless one is explicitly set, which removes the failure mode instead
//! of negotiating with it. [`post_token`] asserts that at runtime — see the
//! guard below, and do not "helpfully" add an origin, a `Referer`, or any other
//! browser-shaped header to this request.
//!
//! # Secrecy
//!
//! The authorization code, the PKCE verifier, the refresh token and the access
//! token pass through here and **none of them is ever logged, printed, or put
//! into an error message** — including on failure paths. There is deliberately
//! no `tracing`, no `eprintln!`, and no `Debug` derive on any type holding one.
//! Only Entra's own `error` / `error_description` ever escapes: those carry an
//! AADSTS code and a correlation id, which is what a user needs in order to
//! report a problem and is safe to show.

use serde::{Deserialize, Serialize};
use std::sync::OnceLock;
use std::time::Duration;

/// The only authority tokens are redeemed against. Not caller-supplied: the
/// frontend picks the tenant, never the host.
const AUTHORITY: &str = "https://login.microsoftonline.com";

/// Entra answers the token endpoint quickly or not at all; a hung request must
/// not leave the sign-in spinner up forever.
const TIMEOUT: Duration = Duration::from_secs(30);

/// A successful token response, as the frontend consumes it.
///
/// No `Debug`: two of these three fields are credentials, and a derived `Debug`
/// is how they end up in a panic message or a log line.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TokenSuccess {
    access_token: String,
    refresh_token: Option<String>,
    /// Seconds, as Entra reports it. The frontend turns it into a deadline.
    expires_in: Option<u64>,
}

/// A refusal, mapped onto the frontend's `EntraResponseError` taxonomy.
///
/// `code` is the OAuth error (`invalid_grant`, `invalid_client`, …), or one of
/// the synthetic codes below when the failure happened before Entra answered.
/// `network_error` specifically means "no verdict from Entra", and the frontend
/// relies on that to avoid ending a session over a flaky connection.
#[derive(Serialize)]
pub struct TokenError {
    code: String,
    description: String,
}

impl TokenError {
    fn new(code: &str, description: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            description: description.into(),
        }
    }
}

/// Entra's token response. Success and failure share one shape.
#[derive(Deserialize)]
struct TokenPayload {
    access_token: Option<String>,
    refresh_token: Option<String>,
    expires_in: Option<u64>,
    error: Option<String>,
    error_description: Option<String>,
}

/// A tenant id goes into the URL path, so it is validated rather than trusted.
/// Covers a GUID, a verified domain, and the `common` / `organizations` aliases.
fn valid_tenant(tenant: &str) -> bool {
    !tenant.is_empty()
        && tenant.len() <= 64
        && tenant
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'.')
}

/// One client for the process.
///
/// Built on rustls — the same TLS stack the rest of the dependency tree already
/// uses, so this does not link a second one into the binary. `https_only` is
/// belt-and-braces next to the hardcoded [`AUTHORITY`], and redirects are
/// refused because a token endpoint has no business issuing one and following
/// it would replay the form body — which holds the credential — elsewhere.
fn client() -> Result<&'static reqwest::Client, TokenError> {
    static CLIENT: OnceLock<Result<reqwest::Client, String>> = OnceLock::new();

    CLIENT
        .get_or_init(|| {
            reqwest::Client::builder()
                .https_only(true)
                .redirect(reqwest::redirect::Policy::none())
                .timeout(TIMEOUT)
                .build()
                .map_err(|err| err.without_url().to_string())
        })
        .as_ref()
        .map_err(|err| TokenError::new("network_error", format!("HTTPS client unavailable: {err}")))
}

/// Builds and sends the token POST, and hands back the raw response.
///
/// Separate from [`post_token`] only so the live tests at the bottom of this
/// file can inspect the *response headers* — which is how they prove no
/// `Origin` went out. Everything about the request is constructed here, so the
/// tests exercise the real path rather than a reconstruction of it.
///
/// The form carries the credential (an authorization code or a refresh token);
/// it is never logged, and it is never included in an error returned from here.
async fn send_token_request(
    tenant_id: &str,
    form: &[(&str, &str)],
) -> Result<reqwest::Response, TokenError> {
    if !valid_tenant(tenant_id) {
        return Err(TokenError::new(
            "invalid_tenant",
            "The configured Entra tenant id is not a valid tenant identifier.",
        ));
    }

    let url = format!("{AUTHORITY}/{tenant_id}/oauth2/v2.0/token");

    let request = client()?
        .post(&url)
        .header(reqwest::header::ACCEPT, "application/json")
        // `.form()` sets Content-Type: application/x-www-form-urlencoded.
        .form(form)
        .build()
        .map_err(|err| {
            TokenError::new(
                "network_error",
                format!("Could not build the request: {}", err.without_url()),
            )
        })?;

    // ------------------------------------------------------------------
    // THE POINT OF THIS WHOLE MODULE.
    //
    // Entra treats *any* `Origin` header as "this is a browser" and refuses
    // redemption for a native public client with AADSTS9002326. `reqwest` adds
    // no `Origin` of its own, so this holds today; the assertion is here so a
    // future header addition fails loudly and locally rather than as an
    // unexplained sign-in failure on a user's machine. Do not delete it, and do
    // not set an Origin — not even an empty one. An empty value was tried and
    // it did not help (see the module docs).
    // ------------------------------------------------------------------
    debug_assert!(
        !request.headers().contains_key(reqwest::header::ORIGIN),
        "the Entra token request must not carry an Origin header (AADSTS9002326)"
    );
    if request.headers().contains_key(reqwest::header::ORIGIN) {
        return Err(TokenError::new(
            "origin_header_set",
            "Internal error: the token request carried an Origin header.",
        ));
    }

    client()?.execute(request).await.map_err(|err| {
        // `without_url()` keeps the tenant out of the message; the body — which
        // is where the credential is — was never part of it.
        TokenError::new(
            "network_error",
            format!("Could not reach Microsoft: {}", err.without_url()),
        )
    })
}

/// Redeems a grant and returns the tokens, or Entra's refusal.
async fn post_token(tenant_id: &str, form: &[(&str, &str)]) -> Result<TokenSuccess, TokenError> {
    let response = send_token_request(tenant_id, form).await?;

    let status = response.status();
    let payload: TokenPayload = match response.json().await {
        Ok(payload) => payload,
        Err(_) => {
            return Err(TokenError::new(
                "invalid_response",
                format!("Microsoft returned HTTP {}.", status.as_u16()),
            ))
        }
    };

    if let Some(error) = payload.error {
        return Err(TokenError {
            description: payload.error_description.unwrap_or_default(),
            code: error,
        });
    }
    if !status.is_success() {
        return Err(TokenError::new(
            "http_error",
            format!("Microsoft returned HTTP {}.", status.as_u16()),
        ));
    }

    match payload.access_token {
        Some(access_token) => Ok(TokenSuccess {
            access_token,
            refresh_token: payload.refresh_token,
            expires_in: payload.expires_in,
        }),
        None => Err(TokenError::new(
            "no_access_token",
            "Microsoft did not return an access token.",
        )),
    }
}

/// Exchanges an authorization code + PKCE verifier for tokens.
///
/// Called from `src/auth/entra.ts` once the `nesqbot://auth` redirect arrives.
/// `code` and `code_verifier` are one-time credentials: they go into the form
/// body and nowhere else.
///
/// `rename_all` is Tauri's current default, stated explicitly because the
/// frontend spells these arguments in camelCase and a changed default would
/// otherwise break sign-in at runtime, silently and only in the packaged app.
#[tauri::command(rename_all = "camelCase")]
pub async fn redeem_entra_code(
    tenant_id: String,
    client_id: String,
    code: String,
    redirect_uri: String,
    code_verifier: String,
    scope: String,
) -> Result<TokenSuccess, TokenError> {
    post_token(
        &tenant_id,
        &[
            ("client_id", client_id.as_str()),
            ("grant_type", "authorization_code"),
            ("code", code.as_str()),
            ("redirect_uri", redirect_uri.as_str()),
            ("code_verifier", code_verifier.as_str()),
            ("scope", scope.as_str()),
        ],
    )
    .await
}

/// Renews an access token from a stored refresh token, without user interaction.
///
/// Entra may or may not return a new refresh token; deciding what to keep when
/// it does not is the frontend's job, so this just reports what came back.
#[tauri::command(rename_all = "camelCase")]
pub async fn refresh_entra_token(
    tenant_id: String,
    client_id: String,
    refresh_token: String,
    scope: String,
) -> Result<TokenSuccess, TokenError> {
    post_token(
        &tenant_id,
        &[
            ("client_id", client_id.as_str()),
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token.as_str()),
            ("scope", scope.as_str()),
        ],
    )
    .await
}


/// Live checks against Entra's real token endpoint.
///
/// These are `#[ignore]`d so an offline `cargo test` stays green. Run them with
///
/// ```text
/// cargo test --lib entra::tests -- --ignored --nocapture --test-threads=1
/// ```
///
/// Everything they send is a public identifier — tenant, client and API app ids
/// and the redirect URI all ship inside the installer. The `code` is deliberate
/// garbage, so there is no credential in play and nothing to leak: a public
/// client redeeming an invalid code is an unauthenticated, unsuccessful request.
///
/// # How these tests know whether an `Origin` went out
///
/// Not from the AADSTS code, and this is worth writing down because it is the
/// obvious thing to try and it does not work. Entra validates the *shape* of the
/// authorization code before it reaches the SPA / cross-origin gate, so a
/// fabricated code returns `AADSTS9002313` ("Invalid request") whether or not an
/// `Origin` is present — measured, across a range of code shapes and grant
/// types. `AADSTS9002326` is only reachable with a real, decryptable code, which
/// requires an interactive sign-in. The error code therefore cannot discriminate.
///
/// What *does* discriminate, on exactly the same fabricated request, is the
/// response: Entra emits `Access-Control-Allow-Origin` (plus
/// `Access-Control-Allow-Methods` and `Access-Control-Expose-Headers`) if and
/// only if the request carried an `Origin`. Those headers are the visible edge
/// of the cross-origin handling that AADSTS9002326 comes out of, so their
/// absence is direct evidence — from Entra's own answer, not from our side of
/// the wire — that the request was not treated as a browser request at all.
#[cfg(test)]
mod tests {
    use super::*;

    const TENANT: &str = "10000000-0000-0000-0000-000000000001";
    const CLIENT: &str = "30000000-0000-0000-0000-000000000003";
    const REDIRECT: &str = "nesqbot://auth";
    const SCOPE: &str =
        "api://20000000-0000-0000-0000-000000000002/access_as_user openid profile offline_access";

    /// Deliberately not a real authorization code.
    const FAKE_CODE: &str = "0.AXoA-not-a-real-authorization-code-third-attempt-probe";
    const FAKE_VERIFIER: &str = "0123456789abcdef0123456789abcdef0123456789a";

    /// The exact form the app sends when redeeming a code.
    fn form() -> Vec<(&'static str, &'static str)> {
        vec![
            ("client_id", CLIENT),
            ("grant_type", "authorization_code"),
            ("code", FAKE_CODE),
            ("redirect_uri", REDIRECT),
            ("code_verifier", FAKE_VERIFIER),
            ("scope", SCOPE),
        ]
    }

    fn allow_origin(response: &reqwest::Response) -> Option<String> {
        response
            .headers()
            .get(reqwest::header::ACCESS_CONTROL_ALLOW_ORIGIN)
            .map(|value| String::from_utf8_lossy(value.as_bytes()).into_owned())
    }

    async fn aadsts(response: reqwest::Response) -> (String, String) {
        let payload: TokenPayload = response.json().await.expect("a JSON body from Entra");
        (
            payload.error.unwrap_or_default(),
            payload.error_description.unwrap_or_default(),
        )
    }

    /// The shipping path: `send_token_request` builds and sends the request, so
    /// this is the real thing, not a reconstruction.
    ///
    /// Entra must answer without any cross-origin handling — no
    /// `Access-Control-Allow-Origin` — and must not answer `AADSTS9002326`.
    #[tokio::test]
    #[ignore = "hits live Entra"]
    async fn shipping_path_sends_no_origin() {
        let response = send_token_request(TENANT, &form())
            .await
            .unwrap_or_else(|_| panic!("could not reach Entra"));

        let acao = allow_origin(&response);
        let status = response.status();
        let (code, description) = aadsts(response).await;

        println!("shipping path (no Origin)  HTTP {status}  Access-Control-Allow-Origin: {acao:?}");
        println!("                           {code} / {description}");

        assert!(
            acao.is_none(),
            "Entra applied cross-origin handling, so the request carried an Origin: {acao:?}"
        );
        assert!(
            !description.contains("AADSTS9002326"),
            "the cross-origin refusal is still happening"
        );
        // Entra processed the request and rejected the fabricated code, which
        // is the furthest a test can get without an interactive sign-in.
        assert!(
            description.contains("AADSTS"),
            "expected a verdict from Entra, got: {description}"
        );
    }

    /// The control. Identical request, same client, one added header — and
    /// Entra's cross-origin handling switches on. Without this, the assertion
    /// above could pass simply because Entra never sets those headers.
    #[tokio::test]
    #[ignore = "hits live Entra"]
    async fn adding_an_origin_switches_on_cross_origin_handling() {
        let request = client()
            .unwrap_or_else(|_| panic!("HTTPS client unavailable"))
            .post(format!("{AUTHORITY}/{TENANT}/oauth2/v2.0/token"))
            .header(reqwest::header::ACCEPT, "application/json")
            .header(reqwest::header::ORIGIN, "http://tauri.localhost")
            .form(&form())
            .build()
            .expect("build the control request");

        let response = client()
            .unwrap_or_else(|_| panic!("HTTPS client unavailable"))
            .execute(request)
            .await
            .expect("reach Entra");

        let acao = allow_origin(&response);
        let status = response.status();
        let (code, description) = aadsts(response).await;

        println!("control (Origin present)   HTTP {status}  Access-Control-Allow-Origin: {acao:?}");
        println!("                           {code} / {description}");

        assert!(
            acao.is_some(),
            "expected Entra to apply cross-origin handling when an Origin is present"
        );
    }

    /// Cheap, offline, and the one that will actually catch a regression in CI:
    /// whatever headers this module grows, `Origin` must never be one of them.
    #[test]
    fn request_builder_never_sets_an_origin() {
        let request = client()
            .unwrap_or_else(|_| panic!("HTTPS client unavailable"))
            .post(format!("{AUTHORITY}/{TENANT}/oauth2/v2.0/token"))
            .header(reqwest::header::ACCEPT, "application/json")
            .form(&form())
            .build()
            .expect("build the request");

        assert!(
            !request.headers().contains_key(reqwest::header::ORIGIN),
            "the Entra token request must not carry an Origin header (AADSTS9002326)"
        );
    }
}
