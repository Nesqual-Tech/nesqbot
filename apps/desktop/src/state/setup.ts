/**
 * Whether the setup wizard has ever been completed on this machine.
 *
 * `localStorage`, not the OS credential store: this is a boolean, not a
 * secret, in the same tier as `auth/storage.ts`'s `USER_CACHE_KEY` and
 * `api/client.ts`'s endpoint override.
 *
 * A cold start with nothing set is what a fresh install looks like, and that
 * is exactly the case the wizard exists for — see `SetupGate` in `App.tsx`.
 * "Completed" does not mean "configured every field"; the wizard can be
 * skipped, and skipping still counts as completed so an install that wants
 * the built-in defaults is not nagged every launch. Reachable again later
 * from the command palette (`act-setup`) or the sidebar footer.
 */
const SETUP_COMPLETE_KEY = "nesq.setup.completed"

export function hasCompletedSetup(): boolean {
  try {
    return localStorage.getItem(SETUP_COMPLETE_KEY) === "1"
  } catch {
    // Storage unavailable (private mode, disabled site data): behave as
    // already-set-up rather than trapping every launch behind a wizard that
    // can never record completion.
    return true
  }
}

export function markSetupComplete(): void {
  try {
    localStorage.setItem(SETUP_COMPLETE_KEY, "1")
  } catch {
    /* nothing else to do; the wizard will simply reopen next launch */
  }
}

/** Only for the "revisit setup" entry points — does not affect `API_BASE`. */
export function resetSetupCompletion(): void {
  try {
    localStorage.removeItem(SETUP_COMPLETE_KEY)
  } catch {
    /* private mode / disabled storage */
  }
}
