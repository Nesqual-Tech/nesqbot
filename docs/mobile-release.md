# Releasing on Apple platforms — iOS and macOS

What is built, what is blocked, exactly what unblocks it, and what each step costs in time
and money. Windows and Android are covered elsewhere (`docs/code-signing.md`,
`docs/deploy.md`); this file is only about the two Apple targets.

**Nothing in this document has been submitted to Apple, and no Apple credential exists in
this repository.** Every step below that needs one is marked and is genuinely not done.

## The short version

|                                             | iOS (Expo / EAS)                           | macOS (Tauri)                                                 |
| ------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------- |
| Config written                              | ✅ `apps/mobile/eas.json`, `app.config.ts` | ✅ `bundle.macOS` in `tauri.conf.json` + `entitlements.plist` |
| Builds today with no Apple account          | ✅ **simulator build**                     | ✅ **unsigned `.app` + `.dmg` on a CI Mac**                   |
| Needs an Apple Developer Program membership | for device / TestFlight / App Store        | for Developer ID signing + notarisation                       |
| Needs a physical Mac                        | ❌ no — EAS builds on cloud Macs           | ❌ no — GitHub-hosted `macos-15` runner                       |
| Blocking cost                               | $99/year                                   | $99/year (same membership) + 10× CI minutes                   |

Neither platform needs a Mac on a desk any more, but for different reasons. EAS runs the Xcode
build on Expo's own macOS machines. Tauri has no equivalent — a `.app`/`.dmg` must still be
produced _on_ macOS — so `.github/workflows/macos.yml` rents one from GitHub Actions instead.
What neither route escapes is the **Apple Developer Program** the moment you want to hand the
result to somebody else.

**The EAS project is now linked** (step 0). `eas build` and `eas config` no longer refuse, and
`getExpoPushTokenAsync` has the id it needs. A notification still does not _arrive_ without the
Apple Developer Program, for a different reason — see the table in step 0.

---

## Step 0 — link the EAS project ✅ **done**

```
Created @YOUR_EXPO_ACCOUNT/nesqbot
Project ID: YOUR_EAS_PROJECT_ID
https://expo.dev/accounts/YOUR_EXPO_ACCOUNT/projects/nesqbot
```

The id lives in `apps/mobile/app.json` under `expo.extra.eas.projectId`. `app.config.ts` still
prefers `process.env.EAS_PROJECT_ID` over it, which is the right precedence for CI.

Two things about how it was run, because both look like bugs later:

- It was invoked from the **repo root**, which wrote a stray root `app.json` containing only the
  project id. Nothing reads that file — `app.config.ts` resolves `config.extra?.eas?.projectId`
  from `apps/mobile/app.json` — and it has been deleted. Run EAS commands from `apps/mobile`.
- It warned `Cannot determine which native SDK version your project uses because the module
'expo' is not installed`. Same root-directory side effect: `apps/mobile` installs separately
  (`npm run install:mobile`) and is deliberately not an npm workspace.

Verified rather than assumed:

```
$ cd apps/mobile && npx expo config --json    # .extra.eas
{ "projectId": "YOUR_EAS_PROJECT_ID" }
```

### What this unblocks, and what it does **not**

`getExpoPushTokenAsync({ projectId })` needs **two** things and the project id is one. The other
is a build carrying real APNs credentials, which needs the Apple Developer Program. Conflating
them sends people debugging the wrong end, so the app now reports them separately:

| State                                         | `PushRegistrationStatus` | What the person is told                                                                                                |
| --------------------------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| No project id                                 | `no-project-id`          | — (no longer reachable)                                                                                                |
| Simulator / no hardware                       | `unsupported`            | "Push notifications need a physical device."                                                                           |
| Real device, no APNs credentials in the build | `no-credentials`         | "This build has no push credentials. It needs a development or TestFlight build signed by an Apple Developer account." |
| Permission refused                            | `denied`                 | offers a link to system settings                                                                                       |
| Token minted and stored                       | `registered`             | "This device will receive approval alerts."                                                                            |

**"A token was obtained" and "a notification arrives" are still different claims, and the second
is still blocked** — no longer on the project id, but on a signed device build. What was actually
verified from Windows, with no Mac and no Apple account:

| Claim                                                | Verified how                           | Result                                                          |
| ---------------------------------------------------- | -------------------------------------- | --------------------------------------------------------------- |
| The project id reaches the built config              | `npx expo config --json`               | ✅ present                                                      |
| `POST /me/devices` exists in production              | `curl` against the live API            | ✅ 401 unauthenticated — the route is there                     |
| The payload the API sends routes to the right screen | `npm run check` (`notificationTarget`) | ✅ asserted against `services/notifications.py`'s exact payload |
| A notification actually arrives on a phone           | —                                      | ❌ **not verified.** Needs a device build.                      |

The last row cannot be closed from this machine: `Device.isDevice` is false in a simulator, and
minting an iOS push token needs an `aps-environment` entitlement that only a signed development
or TestFlight build carries. It is part of the Apple Developer Program dependency below, not a
separate task.

### The gap that is not about Apple

`apps/api/app/services/notifications.py` pushes **approvals only**. A run parked in
`awaiting_human` — an agent stuck at a login — sends nothing, so the phone finds it by polling
`GET /runs?status=awaiting_human`. The app is honest about this (the Settings screen says so) and
sorts takeover cards above approvals in the inbox to compensate. Closing it is a small addition to
that service; see `docs/mobile-parity.md`, Part 4.

## iOS

### What builds right now, with no Apple account

An **iOS Simulator build**. It is unsigned by definition, so Apple is not involved:

```
cd apps/mobile
eas build --platform ios --profile simulator
```

Step 0 is done, so this command is now runnable. It produces a `.app` that runs in the Simulator
on any Mac, and it is the honest answer to "does the iOS app actually work" without spending a
cent. **Not run by this lane** — the output needs a Mac to be worth anything, and there is not one
here.

Already verified locally on Windows, with no Mac and no Apple account — the JavaScript half of
the app compiles end to end:

```
$ npx expo export --platform ios
iOS Bundled 3147ms node_modules\expo-router\entry.js (1126 modules)
› ios bundles (1):
  _expo/static/js/ios/entry-….hbc  (3.04 MB)
```

### What needs the Apple Developer Program

| Goal                     | Profile                   | Needs                                     |
| ------------------------ | ------------------------- | ----------------------------------------- |
| Run on a physical iPhone | `development` / `preview` | membership + a registered device UDID     |
| Internal testers         | `preview` → TestFlight    | membership + App Store Connect app record |
| Public release           | `production`              | membership + App Store review             |

**Cost: $99 USD/year**, Apple Developer Program.

**Time — and this is the part people underestimate.** The bundle identifier is
`com.nesqualtech.nesqbot` and the publisher is _Nesqual Tech SRL_, so this should be an
**organization** account, not an individual one. Organization enrolment requires a
**D-U-N-S number** for the legal entity:

| Step                                             | Who        | Realistic time                                       |
| ------------------------------------------------ | ---------- | ---------------------------------------------------- |
| Obtain / verify D-U-N-S for Nesqual Tech SRL     | a director | **1–14 days** (free from Dun & Bradstreet, but slow) |
| Apple organization enrolment + verification call | a director | 2–7 days after D-U-N-S                               |
| Create App ID + App Store Connect record         | any Admin  | ~30 min                                              |
| First TestFlight build + Apple processing        | developer  | ~1 hour                                              |
| First App Store review                           | Apple      | 1–3 days typical                                     |

An **individual** account enrols in ~24–48h with no D-U-N-S, but the seller name becomes a
person, not the company. That is a business decision, not a technical one — flagging it rather
than choosing it.

### Credentials — let EAS hold them

Once the membership exists:

```
cd apps/mobile
eas credentials            # interactive; sign in with the Apple ID once
eas build --platform ios --profile production
eas submit --platform ios --profile production
```

EAS generates and stores the distribution certificate and provisioning profile on Expo's
servers. **Nothing is written into this repository, and nothing should be.** The Apple ID must
be an Account Holder or Admin on the team the first time.

If the team prefers to keep signing material in-house, `eas.json` also supports
`credentialsSource: "local"` with a `credentials.json` — that file must then be gitignored and
distributed out of band. Not configured here on purpose.

### iOS configuration already in place

- `apps/mobile/eas.json` — `simulator`, `development`, `preview`, `production` profiles,
  `appVersionSource: "remote"` so EAS owns the build number.
- `app.config.ts` — `ios.config.usesNonExemptEncryption: false`, so App Store Connect stops
  asking the export-compliance question on every upload. (The app uses only standard HTTPS.)
- `expo-build-properties` — `ios.deploymentTarget: "15.1"`, the floor for Expo SDK 52 / RN 0.76.
- `app.json` — `UIBackgroundModes: ["remote-notification"]` and `NSFaceIDUsageDescription` are
  already correct for push and for the secure-store unlock.

### One caveat that will bite on the first EAS build

This repository **is not a git repository**. EAS CLI uses git to work out what to upload. On the
first `eas build` either initialise git, or set:

```
EAS_NO_VCS=1 eas build --platform ios --profile preview
```

Left as-is rather than running `git init` on the owner's tree.

---

## macOS

### The headline: no Mac is required any more

Tauri still cannot cross-compile — a `.app` and a `.dmg` can only be produced on macOS, by
`codesign`, `hdiutil` and `notarytool`, none of which exist on Windows. What changed is where
that macOS comes from. **`.github/workflows/macos.yml` builds the desktop app on a GitHub-hosted
macOS runner.** Nobody has to buy hardware to get a real, runnable `.app`.

Run it from the Actions tab (`macOS desktop` → _Run workflow_, which builds any branch), or let
it fire on a push to `main` that touches `apps/desktop/**`, `packages/**` or the workflow itself.
There is **no pull-request trigger**: at a 10× multiplier one macOS build costs about what a
whole day of the Linux CI does, and `packages/**` moves on nearly every UI change.

| Input            | Default     | What it does                                           |
| ---------------- | ----------- | ------------------------------------------------------ |
| `architectures`  | `universal` | `universal` \| `apple-silicon` \| `intel` \| `all`     |
| `force_unsigned` | `false`     | build unsigned even when the Apple secrets are present |

The default is a single **universal** build: `--target universal-apple-darwin`, which compiles
`aarch64-apple-darwin` and `x86_64-apple-darwin` and `lipo`s them into one binary that runs
natively on both Apple silicon and Intel. The per-architecture options exist for bisecting a
failure that only shows up on one slice — they cost the same each, so do not run `all` out of
habit.

Each matrix leg uploads one artifact containing:

- `NesqBot-<version>-macos-<arch>-<signing state>.app.zip` — zipped with `ditto`, because
  `upload-artifact` follows symlinks and drops the executable bit, which silently corrupts a
  `.app`
- the matching `.dmg`
- `SHA256SUMS.txt`

### What "unsigned" means, and why it is still worth having

With no Apple credentials configured the workflow builds anyway and labels everything
`UNSIGNED` — in the artifact name, in the job name, and in a warning box in the run summary.
That is deliberate: an unsigned build is genuinely useful for "does the macOS app actually
work", and genuinely useless for distribution. It must never be mistaken for the latter.

Gatekeeper will refuse to open it. On your own machine:

```
unzip NesqBot-0.2.2-macos-universal-UNSIGNED.app.zip
xattr -dr com.apple.quarantine "Nesq Bot.app"
open "Nesq Bot.app"
```

Deep links (`nesqbot://`) only resolve for an app in `/Applications`, so move it there before
testing the Entra sign-in redirect.

### Signing and notarisation — six secrets, two all-or-nothing groups

Set these as repository secrets. They are read by name only; nothing is written into the repo.

| Group            | Secrets                                                                     | Effect                 |
| ---------------- | --------------------------------------------------------------------------- | ---------------------- |
| **Signing**      | `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY` | Developer ID signature |
| **Notarisation** | `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`                               | Apple notary + staple  |

`APPLE_CERTIFICATE` is a base64 of the exported `.p12`; `APPLE_PASSWORD` is an
**app-specific password** from appleid.apple.com, never the account password. Tauri's bundler
imports the certificate into a temporary keychain by itself — the workflow does no
`security create-keychain` dance.

**A group is all or nothing, and a partial group fails the run before a single macOS minute is
spent.** The `plan` job runs on `ubuntu-24.04` and refuses:

| State                                   | Result                                                    |
| --------------------------------------- | --------------------------------------------------------- |
| no secrets at all                       | ✅ unsigned build, labelled `UNSIGNED`                    |
| all 3 signing secrets                   | ✅ `signed-unnotarized`, with a loud warning              |
| all 6                                   | ✅ `signed-notarized`, stapled ticket asserted            |
| 1 or 2 of the signing group             | ❌ **error** naming the missing secrets                   |
| 1 or 2 of the notarisation group        | ❌ **error** naming the missing secrets                   |
| notarisation secrets but no certificate | ❌ **error** — Apple will not notarise an unsigned bundle |

Signing is additionally limited to `push` and `workflow_dispatch`, so that adding a
`pull_request` trigger later cannot start minting signed binaries or queueing jobs with Apple's
notary service by accident.
`signed-unnotarized` is a supported but nearly useless state — Gatekeeper still blocks a
Developer ID app that has not been notarised — so the workflow warns rather than pretending.

After the build, `signed-*` artifacts are verified rather than trusted: `codesign --verify --deep
--strict` must pass and the hardened-runtime entitlements must be present in the embedded
signature; `signed-notarized` must additionally have a stapled ticket, or the job fails. Tauri
warns and carries on in some signing failure modes, and a mislabelled artifact is worse than a
failed build.

### Cost

| Item                               | Cost                                                                                                             |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Apple Developer Program            | **$99/year** — the same membership as iOS, one covers both                                                       |
| GitHub-hosted macOS minutes        | **10× multiplier** on private repos. A cold ~20 min build ≈ 200 billable minutes; a warm cache roughly halves it |
| A Mac on someone's desk (optional) | ~€600+ one-off, for day-to-day development rather than releases                                                  |

The runner is pinned to `macos-15` (arm64), matching how the other workflows pin `ubuntu-24.04`.
`macos-14` is deprecated; move to `macos-26` when `macos-15` follows it.

The **D-U-N-S dependency applies here too.** Signing as _Nesqual Tech SRL_ needs an
**organization** Apple Developer account, which needs a D-U-N-S number for the legal entity —
1–14 days from Dun & Bradstreet, then 2–7 days for Apple's enrolment and verification call. See
the iOS section above; it is the same enrolment, and it is the long pole for macOS distribution,
not the workflow.

### Timeline once the membership exists

| Step                                               | Time                          |
| -------------------------------------------------- | ----------------------------- |
| Create the Developer ID Application certificate    | ~30 min                       |
| Export `.p12`, base64 it, add 6 repository secrets | ~15 min                       |
| First `signed-notarized` run                       | ~30–45 min (notary adds 2–15) |

Use a **Developer ID Application** certificate, not "Apple Distribution" — the latter is for the
App Store and will not notarise for direct download.

### Config that makes this work

`apps/desktop/src-tauri/tauri.conf.json` carries a `macOS` block alongside the untouched Windows
block:

```json
"macOS": {
  "minimumSystemVersion": "11.0",
  "hardenedRuntime": true,
  "entitlements": "entitlements.plist",
  "dmg": { … }
}
```

`bundle.macOS.signingIdentity` is deliberately absent — the identity comes from
`APPLE_SIGNING_IDENTITY` in the environment, and the workflow fails if anyone ever commits it to
the config file.

`apps/desktop/src-tauri/entitlements.plist` grants `cs.allow-jit` and
`cs.allow-unsigned-executable-memory` (WKWebView under the hardened runtime) plus
`network.client`. It requests **no keychain entitlement**: `src/secrets.rs` keeps the Entra
refresh token and session JWT as generic passwords in the login keychain, and a non-sandboxed app
needs no entitlement for that. It also does not request `app-sandbox` — this is Developer ID
direct distribution, not the Mac App Store. The file's own comments carry the detail.

`Cargo.toml` already selects the Keychain backend per target:

```toml
[target.'cfg(target_os = "macos")'.dependencies]
keyring = { version = "3", features = ["apple-native"] }
```

`apps/desktop/src-tauri/Cargo.lock` is now committed, so CI resolves the same dependency graph
that was checked here instead of picking up whatever is newest that morning.

### Two things about the CI build that will surprise you

- **The DMG has no custom window layout.** GitHub sets `CI=true`, and Tauri responds by passing
  `--skip-jenkins` to `bundle_dmg.sh`, which skips the AppleScript that positions the icons.
  Without it the script hangs waiting for a Finder that does not exist on a headless runner. The
  `bundle.macOS.dmg` `windowSize`/`appPosition` settings therefore apply only to a DMG built on a
  real desktop session. The disk image is otherwise fine.
- **The Rust target directory is cached**, ~3 GB per matrix leg against a 10 GB per-repository
  cache budget. Running `all` regularly will thrash it. The comment in the workflow says what to
  drop if that happens.

### Building on a Mac, if one ever appears

```
npm run install:desktop
cd apps/desktop && npx tauri build --bundles app,dmg
```

Same configuration, same result, no CI minutes.

### Not done, deliberately

- **Auto-update.** `bundle.createUpdaterArtifacts` and the updater plugin are not configured for
  either platform. Adding it needs a signing keypair and a hosting endpoint; it is its own task.
- **App Store Connect API key notarisation.** Tauri also accepts `APPLE_API_ISSUER` /
  `APPLE_API_KEY` / `APPLE_API_KEY_PATH`, which is the better CI credential because it is scoped
  and revocable without touching the Apple ID. The workflow implements the Apple ID route because
  that is the trio named in the brief; adding the API key route is a small change to the two
  groups in the `plan` job.
- **Publishing a release.** The workflow uploads build artifacts. It does not create a GitHub
  Release, and it does not push anything anywhere.

---

## Who has to do what

| Task                                            | Who                         | Blocked on              |
| ----------------------------------------------- | --------------------------- | ----------------------- |
| `eas init --account YOUR_EXPO_ACCOUNT`                  | anyone with the Expo login  | nothing — do this first |
| Decide individual vs organization Apple account | a director                  | business decision       |
| Obtain D-U-N-S (org route only)                 | a director                  | Dun & Bradstreet        |
| Enrol in Apple Developer Program, pay $99       | a director / Account Holder | the above               |
| `eas credentials`, first TestFlight build       | developer                   | membership              |
| Approve macOS CI minutes (10× multiplier)       | whoever owns the budget     | —                       |
| Developer ID cert + 6 repository secrets        | developer                   | membership              |

## Verification log

What was actually run for this lane, on Windows, with no Apple credentials:

| Check                                                 | Result                                    |
| ----------------------------------------------------- | ----------------------------------------- |
| `eas whoami`                                          | `YOUR_EXPO_ACCOUNT` / `avery@example.test` |
| `eas project:info`                                    | **linked** — `YOUR_EAS_PROJECT_ID`    |
| `npx expo-doctor`                                     | **18/18 passed**                          |
| `npx expo export --platform ios`                      | **1126 modules, 3.04 MB Hermes bundle**   |
| `npx tsc --noEmit` (mobile)                           | clean, incl. `--noUnusedLocals`           |
| `npx tsc --noEmit` (mobile, typed routes generated)   | clean — every `router.push` resolves      |
| `npm run check` (mobile)                              | **69 checks** against the real source     |
| `npx tsc -p tsconfig.json --noEmit` (desktop)         | clean                                     |
| `node packages/protocol/scripts/check-api-parity.mjs` | `ok — 35 models match`                    |
| `npx tauri info`                                      | parses the `macOS` block without error    |
| Anything submitted to Apple                           | **nothing**                               |

### macOS workflow — verified from Windows

| Check                                                      | Result                                                     |
| ---------------------------------------------------------- | ---------------------------------------------------------- |
| `macos.yml` parses as YAML                                 | ok                                                         |
| `macos.yml` against the GitHub Actions workflow schema     | valid (`action-validator`)                                 |
| Every inline `run:` block                                  | `bash -n` clean                                            |
| `prettier --check` on all four workflows                   | already conformant                                         |
| `plan` job matrix logic, all 5 inputs                      | executed locally — correct JSON, bad input exits 1         |
| `plan` job signing logic, all 10 secret combinations       | executed locally — 3 pass, 3 hard-fail, PR/force downgrade |
| `tauri.conf.json` against Tauri's own `Config` JSON schema | **validates**; `bundle.macOS` validates as `MacConfig`     |
| `entitlements.plist`                                       | parses as a plist; 3 expected keys                         |
| `cargo check --target aarch64-apple-darwin`                | full graph resolves, dies at `cc` (no C toolchain)         |
| `cargo check --target x86_64-apple-darwin`                 | same — resolves, dies at `cc`                              |
| `bundle.windows` `signCommand`                             | untouched, still present                                   |

### macOS workflow — unproven until it runs

Everything that needs a Mac. In particular: whether `tauri build` completes on `macos-15`;
whether the universal `lipo` step succeeds; whether the DMG bundler behaves; whether
`CFBundleURLTypes` really receives the `nesqbot` scheme (the Windows NSIS template consumes the
same `deep_link_protocols` setting and does register it, which is why the workflow asserts it);
and the entire signing and notarisation path, for which no credential exists yet.

A first run that fails is expected. A first run that is claimed to have passed without one
having happened is not.
