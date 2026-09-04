/**
 * The sidebar's account control: sign in with Microsoft, or show who is signed
 * in and offer to sign out.
 *
 * This is a control, not a login screen: the login screen is `SignInScreen`,
 * which `AuthGate` in `App.tsx` puts in front of the whole workspace whenever a
 * session is actually required. Signed out, this control only appears in a
 * development build, where the API's `X-Nesq-Dev` bypass still works and
 * signing in is genuinely optional.
 *
 * Nothing here ever renders anything derived from the redirect URL — see
 * `auth/entra.ts`. Only the error's message reaches the UI, and the only errors
 * that carry text from Microsoft are its `error_description`, which is an
 * AADSTS code and a correlation id.
 */
import { useCallback } from "react"
import { EntraCancelledError, useAuth } from "../auth"
import { errorMessage } from "../api/client"
import { ShellUnavailableError } from "../lib/tauri"
import { useToast } from "../state/AppState"
import { Icon } from "./Icon"

export function AccountBox() {
  const { status, user, signingIn, entraAvailable, signInWithEntra, signOut } = useAuth()
  const toast = useToast()

  const onSignIn = useCallback(() => {
    void signInWithEntra().catch((caught: unknown) => {
      if (caught instanceof EntraCancelledError) return
      if (caught instanceof ShellUnavailableError) {
        toast.info("Microsoft sign-in", "Only available in the installed desktop app.")
        return
      }
      toast.error("Microsoft sign-in failed", errorMessage(caught))
    })
  }, [signInWithEntra, toast])

  const onSignOut = useCallback(() => {
    void signOut().then(() => toast.info("Signed out", "This device only; the Microsoft session is untouched."))
  }, [signOut, toast])

  if (status === "loading") {
    return (
      <span className="status-line" role="status">
        Restoring session…
      </span>
    )
  }

  if (status === "authenticated" && user) {
    return (
      <div className="account">
        <span className="account__name" title={user.email}>
          <Icon name="user" size={13} />
          {user.display_name || user.email}
          {user.role === "admin" ? (
            <span className="account__role" title="Admin: may edit shared bots and the connector catalog">
              admin
            </span>
          ) : null}
        </span>
        <button type="button" className="btn btn--ghost btn--sm" onClick={onSignOut}>
          Sign out
        </button>
      </div>
    )
  }

  if (!entraAvailable) return null

  /*
   * A control, not a screen. The designed signed-out surface is
   * `SignInScreen`, which `AuthGate` puts in front of the whole app; this only
   * ever shows in a development build, where the API's dev bypass means the
   * workspace works signed out and signing in is optional.
   */
  return (
    <button type="button" className="btn btn--ghost btn--sm" onClick={onSignIn} disabled={signingIn}>
      <Icon name="user" size={13} />
      {signingIn ? "Waiting…" : "Sign in"}
    </button>
  )
}
