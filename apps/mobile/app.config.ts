/**
 * Dynamic Expo config. Layers build-time values on top of the static `app.json`.
 *
 * WHY THIS FILE EXISTS — the localhost bug
 * ----------------------------------------
 * `src/api/client.ts` resolves the API base URL as:
 *
 *     settings override  >  expoConfig.extra.apiUrl  >  EXPO_PUBLIC_API_URL  >  fallback
 *
 * `extra.apiUrl` therefore WINS over the environment variable. `app.json` used to hard
 * code it to `http://localhost:8080/api`, which meant every build — development,
 * preview and production alike — shipped pointing at a server that does not exist on a
 * phone, and `EXPO_PUBLIC_API_URL` could not override it. That is fixed here by making
 * `extra.apiUrl` a computed value: it is whatever the environment says, and the DEFAULT
 * is production, matching the desktop app.
 *
 * To develop against a local API, set `EXPO_PUBLIC_API_URL` (see `.env.example`) — you
 * must opt in to localhost, not out of it.
 */
import type { ConfigContext, ExpoConfig } from "expo/config"

/**
 * The live API. Must stay in step with THREE other places:
 *   - apps/desktop/.env.production            (VITE_API_URL)
 *   - apps/desktop/src-tauri/tauri.conf.json  (connect-src / img-src / frame-src CSP)
 *   - infra deployment
 */
const PRODUCTION_API_URL = "https://your-api.example.com/api"

/**
 * EAS sets this on every build; `eas.json` sets it per profile. Locally it comes from
 * `.env`. Anything other than "production" is treated as a non-production build.
 */
const profile = process.env.EAS_BUILD_PROFILE ?? process.env.APP_VARIANT ?? "development"
const isProduction = profile === "production"

/**
 * Android blocks plain HTTP from API 28 up. Production is HTTPS so it never needs an
 * exemption; development and preview builds are routinely pointed at a plain-HTTP LAN
 * address, so they get one. This replaces the bare `android.usesCleartextTraffic` key
 * that used to sit in `app.json` — that key is not part of the Expo config schema and
 * was failing `expo-doctor`; `expo-build-properties` is the supported way to set it.
 */
const androidCleartext = !isProduction

export default ({ config }: ConfigContext): ExpoConfig => ({
  ...config,
  name: config.name ?? "Nesq Bot",
  slug: config.slug ?? "nesqbot",

  ios: {
    ...config.ios,
    config: {
      ...config.ios?.config,
      // No custom cryptography beyond standard HTTPS. Declaring it here avoids the
      // export-compliance question on every single App Store Connect upload.
      usesNonExemptEncryption: false,
    },
  },

  plugins: [
    ...(config.plugins ?? []),
    [
      "expo-build-properties",
      {
        ios: {
          // Expo SDK 52 / React Native 0.76 require iOS 15.1 as a floor.
          deploymentTarget: "15.1",
        },
        android: {
          usesCleartextTraffic: androidCleartext,
        },
      },
    ],
  ],

  extra: {
    ...config.extra,
    /**
     * Production by default. `EXPO_PUBLIC_API_URL` overrides it — that is the ONLY way
     * to get a build pointing anywhere else, including at localhost.
     */
    apiUrl: process.env.EXPO_PUBLIC_API_URL || PRODUCTION_API_URL,
    eas: {
      ...(config.extra?.eas as Record<string, unknown> | undefined),
      /**
       * Required by `getExpoPushTokenAsync()`, so push notifications — the whole point
       * of this app on a phone — do not work until it is set. It is written here by
       * `eas init`; until then push registration reports `no-project-id` and the app
       * degrades to in-app approvals. See docs/mobile-release.md.
       */
      projectId: process.env.EAS_PROJECT_ID || (config.extra?.eas as { projectId?: string } | undefined)?.projectId,
    },
  },
})
