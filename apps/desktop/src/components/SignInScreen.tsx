/**
 * The signed-out surface.
 *
 * This exists because the app used to bootstrap bots, threads and approvals on
 * mount whether or not anybody was signed in. Against the live API — which runs
 * with `NESQ_ENV=production` and therefore refuses the `X-Nesq-Dev` bypass —
 * every one of those calls came back 401, and the owner's first sight of the
 * product was three panels each rendering the API's raw error detail, "missing
 * bearer token to access api". A cold start is the first impression; it should
 * be a designed screen that asks for a sign-in, not a wall of failed requests.
 *
 * Nothing here touches the auth flow. It calls the same `signInWithEntra` the
 * sidebar control called; only the presentation is new.
 */
import { useCallback } from "react"
import { brand } from "@nesqbot/ui"
import { API_BASE, errorMessage } from "../api/client"
import { EntraCancelledError, useAuth } from "../auth"
import { ShellUnavailableError } from "../lib/tauri"
import { NesqualLockup } from "./Brand"
import { Icon } from "./Icon"
import { Spinner } from "./Spinner"

/**
 * The first label of the API host — `nesqbot-api` rather than the
 * sixty-character Azure Container Apps FQDN. That prefix is the part that says
 * *which* deployment this build talks to, which is the first thing worth
 * knowing when someone reports that nothing loads. The full URL is the tooltip.
 */
function apiLabel(): string {
  try {
    const { host } = new URL(API_BASE)
    return host.split(".")[0] || host
  } catch {
    return API_BASE
  }
}

export function SignInScreen() {
  const { signingIn, error, signInWithEntra, clearError } = useAuth()

  const onSignIn = useCallback(() => {
    clearError()
    void signInWithEntra().catch(() => {
      // The provider has already stored whatever is worth showing; a cancelled
      // sign-in stores nothing, which is the correct outcome for a user who
      // closed the browser tab.
    })
  }, [signInWithEntra, clearError])

  const showError = error !== null && !(error instanceof EntraCancelledError)
  const shellProblem = error instanceof ShellUnavailableError

  return (
    <div className="signin-screen">
      <main className="signin-screen__card">
        <NesqualLockup size={34} wordmark="continuation" tagline title={brand.companyName} />

        <div className="signin-screen__eyebrow">{brand.productName}</div>

        <h1 className="signin-screen__headline">Always-on teammates, under your control.</h1>

        <p className="signin-screen__body">
          Every bot runs in its own isolated desktop and stops for your approval before anything consequential leaves
          the building. Sign in to reach your workspace.
        </p>

        <button
          type="button"
          className="btn btn--primary signin-screen__action"
          onClick={onSignIn}
          disabled={signingIn}
        >
          {signingIn ? (
            <Spinner inline label="Waiting for your browser…" />
          ) : (
            <>
              <Icon name="user" size={16} />
              Sign in with Microsoft
            </>
          )}
        </button>

        {showError ? (
          <div className="signin-screen__error" role="alert">
            <Icon name="alert" size={15} />
            <span>
              {shellProblem ? "Microsoft sign-in is only available in the installed desktop app." : errorMessage(error)}
            </span>
          </div>
        ) : null}

        <footer className="signin-screen__footer">
          <span>{brand.companyLegalName}</span>
          <span className="signin-screen__api" title={API_BASE}>
            {apiLabel()}
          </span>
        </footer>
      </main>
    </div>
  )
}

/** Cold-start placeholder while the stored session is being restored. */
export function SessionBootScreen() {
  return (
    <div className="signin-screen">
      <div className="signin-screen__boot">
        <NesqualLockup size={26} title={brand.companyName} />
        <Spinner inline label="Restoring your session…" />
      </div>
    </div>
  )
}
