/// <reference types="vite/client" />

/**
 * The design-token stylesheet, generated at build time from `@nesqbot/ui` by the
 * `nesq-design-tokens` plugin in `vite.config.ts`. It has to be a build artefact
 * rather than a runtime `<style>`: Tauri nonces `style-src`, which makes CSP
 * ignore `'unsafe-inline'` and silently drop anything a script injects.
 */
declare module "virtual:nesq-tokens.css"

interface ImportMetaEnv {
  /** Base URL of the Nesq Bot API, including the `/api` prefix. */
  readonly VITE_API_URL?: string

  /**
   * Microsoft Entra sign-in. All four are **public** values — they are baked
   * into the installer, so none of them may ever be a secret, and the app has
   * no client secret at all (docs/entra-setup.md).
   *
   * All four have defaults pointing at the live Nesqual Tech registration, so a
   * stock build signs in with no configuration; set these only to build against
   * a different tenant. Defaults live in `src/auth/entra.ts`.
   */
  readonly VITE_ENTRA_TENANT_ID?: string
  /** The **public client** app id (`Nesq Bot`), not the API's. */
  readonly VITE_ENTRA_CLIENT_ID?: string
  /** Full scope URI: `api://<api app id>/access_as_user`. */
  readonly VITE_ENTRA_SCOPE?: string
  /** Must be a redirect URI registered on the client app. */
  readonly VITE_ENTRA_REDIRECT_URI?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
