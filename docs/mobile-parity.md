# Desktop / mobile parity

"We need them to be the same." This document says exactly how much of that is now true, what
is deliberately _not_ the same and why, and what is still outstanding.

Two different questions hide inside "the same", and they have different answers:

- **Code parity** — do both apps derive from one definition of the API contract and one
  definition of the design language? This should be _yes, absolutely_, and is now essentially
  yes.
- **Feature parity** — does every screen exist on both? This should **not** be an unqualified
  yes, and the second half of this document argues each case.

---

## Part 1 — Code parity

### What was actually wrong

The brief for this lane said `apps/mobile` had "its own `src/theme`, its own `src/api`" and
shared nothing. That was **out of date, and the truth was more interesting**: the mobile
_source_ had already been migrated — 15 imports of `@nesqbot/protocol` and 7 of `@nesqbot/ui`
across the screens — but `apps/mobile/package.json` **declared neither package**.

So the imports resolved by luck, through two coincidences:

1. npm links _every_ workspace into the root `node_modules/@nesqbot/` regardless of who
   declares it, and Node's resolution walks up to the root.
2. `metro.config.js` carried a hand-written `extraNodeModules` alias pointing at
   `packages/*/src`.

Neither is a contract. Remove the alias, or install the app on its own, and the mobile app
stops building — which is exactly what happened the moment this lane tried to build it.

### What is shared now

| Concern                             | Before                         | Now                                                        |
| ----------------------------------- | ------------------------------ | ---------------------------------------------------------- |
| API types                           | imported, **undeclared**       | declared dependency, `@nesqbot/protocol`                   |
| Risk-gated action union + narrowing | **hand-copied into mobile**    | deleted; protocol's canonical version                      |
| Colour palettes                     | `@nesqbot/ui`                  | unchanged — already shared                                 |
| Spacing / radii                     | `@nesqbot/ui`                  | unchanged — already shared                                 |
| Type scale                          | **not available to RN at all** | `@nesqbot/ui` `typeScale` via adapter                      |
| Elevation / shadow                  | **not available to RN at all** | `@nesqbot/ui` `elevation` via adapter                      |
| Motion (durations, easing)          | **not available to RN at all** | `@nesqbot/ui` `durations`/`easings` via adapter            |
| Reduce-motion handling              | none                           | `AccessibilityInfo` → the tokens' `reducedMotion` contract |

Hardcoded colour values remaining in `apps/mobile`: **zero**. (One `#8499d9` survives, in the
`expo-notifications` Android accent in `app.json` — a build-time plist value that cannot import
TypeScript. It is the same value as the dark accent token.)

### The duplicate that was deleted

`apps/mobile/src/api/types.ts` declared its own `ActionResult`, `ExecutedActionOut` and
`asPendingApproval()`. Tonight `packages/protocol` grew the canonical versions — with a comment
noting that both lanes had independently invented them, which is the signal it belonged in the
shared package.

Mobile's copy was not merely redundant, it had **already drifted**: it typed `approval_id` as
nullable, the protocol types it as required _because it is the discriminant the outcome union
branches on_. Mobile's copy would have happily produced a "held for approval" object with no
approval id in it. It is gone; the file now only forwards.

This also fixed a live type error — mobile stopped typechecking the moment the protocol
tightened, which is the system working as intended.

### The adapter, and why it is not a fork

`packages/ui` is genuinely platform-neutral and was clearly written with React Native in mind:
`typeScale` uses **absolute** `lineHeight`/`letterSpacing` "because React Native has no em",
and `elevation` ships an `androidElevation` field. Colours, spacing and radii cross the
boundary untouched.

Exactly three things cannot cross, and `apps/mobile/src/theme/tokens.ts` adapts them. Every
value still originates in `@nesqbot/ui`:

| Token        | Web form                             | RN needs                 | How the adapter bridges it                                                    |
| ------------ | ------------------------------------ | ------------------------ | ----------------------------------------------------------------------------- |
| `fontFamily` | CSS stack `'"Inter", "Segoe UI", …'` | one family name          | maps the stack; see the font gap below                                        |
| `easing`     | `cubic-bezier(0.2, 0, 0, 1)`         | `Easing` function        | **parses the control points out of the same string** — the curve cannot drift |
| shadow       | CSS `box-shadow` shorthand           | discrete `shadow*` props | rebuilt from the structured `elevation` token, not from the string            |

Parsing the bezier rather than re-typing the four numbers is the point of the whole exercise: a
change in `packages/ui` reaches the phone with no second edit.

`packages/ui/src/**` was not modified — it is the design lane's tonight, and it did not need to
be.

### The build problem this uncovered

Declaring the dependency broke the build, and the reason matters.

`apps/desktop` needs **React 19**. React Native 0.76 needs **React 18.3.1**. With
`apps/mobile` in the npm `workspaces` array, npm hoists React 19 to the root and is then forced
to nest the entire Expo tree under `apps/mobile/node_modules` to keep React 18 next to it — but
it _still_ hoists `babel-preset-expo` and `@expo/metro-config` to the root, where they can no
longer resolve `expo`:

```
SyntaxError: [BABEL]: Cannot find module 'expo/config'
Require stack:
- C:\nesqbuild\node_modules\babel-preset-expo\build\expo-inline-manifest-plugin.js
```

Nothing bundles — not locally, and not on EAS, which installs the same tree.

**Fix:** `apps/mobile` is no longer an npm workspace. It installs on its own and declares the
shared packages as `file:../../packages/*`, which `.npmrc` `install-links=false` resolves to
**symlinks**. The reasoning is recorded in the `"//workspaces"` note in the root
`package.json`.

This is _better_ parity, not a retreat: the packages are now real, declared, symlinked
dependencies that resolve through ordinary node_modules lookup. The proof is that both the
Metro alias and the tsconfig `paths` mapping could be **deleted**, and the iOS bundle came out
byte-identical (same content hash) and `tsc` stayed clean.

### Deliberate difference: the brand typeface

Desktop loads Inter and Poppins. Mobile renders in the platform system face (San Francisco /
Roboto).

This is honest rather than accidental. The app has no `expo-font` dependency and bundles no font
files, so naming `"Inter"` in a React Native style would be a **lie** — iOS silently ignores an
unknown family and Android falls back to Roboto, with nobody having chosen the result. The
adapter therefore maps the sans and display stacks to the system face explicitly, and only the
monospace stack names a real installed face (`Menlo` / `monospace`), because there the shape
carries meaning.

**To close it:** add `expo-font`, bundle the two `.ttf` files, register them, and return the real
family names from `fontFamilyFor()`. That is one contained change in one function — roughly half
a day including checking the licences ship correctly. Worth doing, not worth blocking on.

### What this lane added to `packages/protocol`

The previous lane's rule — "if the protocol owns a shape, the app only forwards it" — held. What
it could not do was cover shapes the protocol did not have yet. Four features had landed on the
API and **not** in the package, so both clients had invented private versions:

| Shape                         | Where it was                               | Where it is now                                                    |
| ----------------------------- | ------------------------------------------ | ------------------------------------------------------------------ |
| `takeover` SSE event          | `apps/desktop/src/types.ts`, hand-declared | `TakeoverEventData` + a real `takeover` arm on both channel unions |
| `POST /runs/{id}/resume`      | `apps/desktop/src/types.ts`, hand-declared | `ResumeRunRequest` / `ResumeRunResponse`                           |
| `DesktopStreamTicket`         | `apps/desktop/src/types.ts`, hand-declared | `DesktopStreamTicket` next to `BotDesktop`                         |
| `RunStatus: "awaiting_human"` | widened locally as `ParkedRunStatus`       | in `RunStatus`, with `PARKED_RUN_STATUSES`                         |

The desktop lane's own comments asked for exactly this — _"Declared here rather than in
`packages/protocol` only because this lane does not own that package… the mobile app will want
the same three shapes as soon as it grows a resume button."_ It grew one, so they moved.

Also filled in, because the API emits them and the package did not declare them:

- `HandoffEventData` — the five delegation keys (`from_bot_id`, `from_bot_name`, `run_id`,
  `chain`, `delegated`). A client reading only `bot_name` cannot tell a routing hop from one bot
  deliberately handing work to another, which is the more interesting half.
- `ApprovalEventData.phase` / `run_id` — the same event name means "parked" without them and
  "released, and here is which way" with them.
- `DoneEventData.run_id` / `awaiting_human` — how a client that reconnected mid-turn learns the
  turn ended by parking on a person rather than by finishing.
- `desktop` and `cost` arms, matching `PUBLISHED_EVENTS` in the orchestrator.
- `ApprovalExecution` grew a **third arm**. See "The bug the third arm fixes" below.

All of it is additive, and `apps/desktop` still typechecks untouched — verified, not assumed.
The desktop's `parseTakeoverEvent` was written to accept either the `unknown` arm or a real one,
so it keeps working with no flag day.

**The parity script now covers two more models** (`ResumeRunOut`, `DesktopStreamTicketOut`) —
35 rather than 33. Adding them exposed a parser bug: `parsePydantic` did not skip docstrings, so
a wrapped line of prose beginning `instead:` was read as a field and reported as missing from the
TypeScript. Docstrings are skipped now; the TypeScript side already stripped its comments.

### The bug the third arm fixes

`ApprovalExecution` was `{ok: true, result?} | {ok: false, error}`. The API produces a third
shape and has done since the continuation landed: a **rejection that resumed a parked run** comes
back as `{continuation: {…}}` with **no `ok` at all**, because nothing was executed.

Every client that branched `execution.ok ? "OK" : "FAILED"` therefore rendered a perfectly
ordinary rejection as an execution failure. Mobile did exactly that. The union now has the third
arm and `approvalExecutionOutcome()` narrows it in one place; `src/lib/__checks__/smoke.mjs`
asserts all three. The arm is shaped so the desktop's existing ternaries still compile.

### Not migrated: the call sites

The type scale is now _available_ to mobile, but the screens still carry ~75 literal `fontSize`
values (14 distinct sizes, against the scale's 12 steps) and 5 literal `letterSpacing` values.

These were **not** mechanically rewritten, on purpose. The mobile sizes (10–34) do not map
one-to-one onto the scale (11–44), so every substitution is a visual judgement, and this lane
cannot see the screens. Rewriting them blind would have shipped 75 unreviewed visual changes
under the banner of "parity".

The five `letterSpacing` sites are the ones that matter most — `tracking.widest` is literally
measured off the Nesqual logo's tagline, and it is what makes an eyebrow read as Nesqual. Those
are the first thing to migrate when someone can look at a simulator.

---

## Part 2 — Feature parity, per surface

"The same" cannot mean "identical". A desktop is where you _work with_ the team; a phone is where
you _unblock_ it. Every decision below follows from that one sentence, and each is stated so it
can be argued with rather than merely reported.

The thesis, concretely: **the app opens on a queue of things that are stopped and only you can
restart.** Everything else on the phone is either context for that decision or was left out.

### The tab bar is the argument

Five tabs is the iOS limit before the system collapses the rest into "More", so the bar is exactly
full and anything new displaces something.

| #   | Tab          | Why it earns a slot                                                                           |
| --- | ------------ | --------------------------------------------------------------------------------------------- |
| 1   | **Inbox**    | Held approvals **and** runs parked on a human, in one list. The reason the app exists.        |
| 2   | **Bots**     | Who is on the team, what they cost, and the two ways to look at one before you decide.        |
| 3   | **Work**     | What each bot is _holding_, and the handover ledger. A glance, which is a phone's best shape. |
| 4   | **Usage**    | Spend against cap — and now the ability to raise a cap that has stopped a bot.                |
| 5   | **Settings** | Sign-out, API override, notification state.                                                   |

**The Approvals tab is gone**, folded into the Inbox. Two separate queues for "a bot needs a yes"
and "a bot needs you at the screen" is a distinction the _implementation_ cares about and the
person does not: both mean something stopped. The bot list moved from first to second, which is
the visible half of the same argument.

### Per surface

| Surface                       | Desktop              | Mobile                             | Verdict                             |
| ----------------------------- | -------------------- | ---------------------------------- | ----------------------------------- |
| Sign-in (Entra, PKCE)         | ✅                   | ✅                                 | **parity — required**               |
| Approvals list + decide       | ✅ `ApprovalsPanel`  | ✅ merged into Inbox               | **parity — the point of the phone** |
| Approval → _task continues_   | ✅                   | ✅ new                             | **parity**                          |
| Human takeover / Continue     | ✅ `TakeoverCard`    | ✅ new `takeover/[runId]`          | **parity — built this lane**        |
| Push → approval               | ❌ n/a               | ✅ deep-links to the item          | **mobile-only, deliberately**       |
| Bot list / status             | ✅ `Sidebar`         | ✅ `(tabs)/bots`                   | **parity**                          |
| Chat + SSE streaming          | ✅ `ChatPane`        | ✅ `chat/[botId]`                  | **parity**                          |
| Bot-to-bot delegation in chat | ✅                   | ✅ new                             | **parity**                          |
| Desktop cold-start progress   | ✅                   | ✅ new                             | **parity**                          |
| Usage / spend                 | ✅ `UsagePanel`      | ✅ + raise-the-cap                 | **mobile slightly ahead**           |
| Live bot desktop viewer       | ✅ `DesktopPane`     | ⚠️ watch-first, now actually works | **deliberately reduced**            |
| Work items + transfer ledger  | ❌ none yet          | ✅ new `(tabs)/work`               | **mobile-only, for now**            |
| Work item authoring           | ❌ none yet          | ❌                                 | **deliberately desktop-only**       |
| Routines create / record      | ✅                   | ❌                                 | **deliberately desktop-only**       |
| Connectors / MCP management   | ✅                   | ❌                                 | **deliberately desktop-only**       |
| Bot builder                   | ✅                   | ❌                                 | **deliberately desktop-only**       |
| Rehearsal / plans / undo      | ✅ (API), partial UI | ❌                                 | **deliberately left, see below**    |
| Memory / KB                   | ✅                   | ❌                                 | **deliberately desktop-only**       |

### The reasoning, surface by surface

**Approvals — kept, and finished.** A held action is time-sensitive and the decision is small:
read a title, a risk class, a payload summary, decide. That is a perfect phone interaction and a
mediocre desktop one, because at a desk you were already looking at the screen.

What was missing was the _consequence_. Deciding an approval does not just record a decision — it
hands the bot its answer and the parked run **picks the same task back up**. Without saying so, a
person who approves one step of a thirty-step task has no idea whether the other twenty-nine are
still going to happen. The detail screen now renders the continuation: whether the task resumed,
whether it had already moved on (`continued: false` is a double-press, not a failure), and — when
the resumed run immediately parks again on a person — a button straight to the takeover screen.

**Human takeover — built, and it is the best thing in the app.** The agent hits a login it cannot
pass, hands you the screen, and one button resumes the same task. On a laptop that is convenient.
On a phone it is the difference between a task finishing this afternoon and a task finishing
tomorrow, because the person who can pass that login is usually not at their desk. Three things
the screen gets right on purpose:

- `resumed: false` is rendered as "already going", never as an error. Showing an error there
  teaches people to press the button twice, which is precisely what the API's conditional status
  claim is defending against.
- It **refuses to pretend** when the desktop is not running. Resume deliberately does not
  auto-start one, because the value of the handover is the session you just signed into and a
  restart takes the container filesystem with it. The screen says so and offers Start as a
  separate, explicit act.
- The viewer is `view_only` until you turn takeover on. A stray tap on a live, signed-in browser
  is not a gesture anyone intended.
- The Continue button lives in a **pinned footer**, not at the bottom of the scroll. It is the one
  thing the screen is for and it must be under a thumb without scrolling past a video stream.

**There is no push for a takeover.** `services/notifications.py` pushes approvals and nothing
else, so a parked run is found by polling `GET /runs?status=awaiting_human` — which the inbox
does, and which is why the takeover cards are listed _first_. The Settings screen says this
plainly rather than letting someone assume their phone will buzz. Adding a takeover push is an
API-side change and this lane does not own that file.

**The desktop viewer — kept, reduced, and repaired.** The argument for dropping it is strong: a
1080p Linux desktop on a 390pt screen is unreadable and touch is a bad mouse. The argument for
keeping it is stronger: when you are asked to approve "click Send in this window", you must be
able to _see the window_. So it stays as **evidence for a decision**, not as a remote workstation.

It was also outright broken. It pointed a `WebView` at `BotDesktop.stream_url`, which is a
`10.60.x.x` address on the delegated subnet — no phone can route to that on any network, so the
live stream failed **every single time** and silently fell through to the screenshot poller. The
live view was dead code that looked like a feature. It now mints a ticket
(`POST …/desktop/stream/ticket`) and loads the API's own proxy, the same route the desktop app
uses. The screenshot poller stays as the honest fallback, and now says it is one.

**Work items — included, read-mostly.** The transfer ledger is the stated differentiator, and
"which of my agents is sitting on what, and how long has it been sitting there" is a glance, which
is the phone's best shape. Two details the screen refuses to blur:

- `last_event_at` ("heard back 4d ago") is shown in preference to `updated_at` ("updated 4d
  ago"). Only the first means the outside world acted, and collapsing them hides exactly the rows
  worth chasing.
- Transfer requires a reason, with no placeholder and no default. The API requires
  `min_length=1`; a ledger of timestamps without reasons is what the differentiator _is not_.
  `transferred: false` is reported as "already had this", never as a handover that did not happen.

Creating and editing work items is absent: that is authoring. Note the API agrees — `PATCH`
answers **422** to `owner_bot_id` rather than dropping it, because `/transfer` is the only path
that writes the ledger, so there is no "quick reassign" shortcut to build even if one seemed handy.

**Usage — kept, and given the one write that matters.** A bot at its cap is a _stopped agent_,
which is the one thing this app exists to restart, and the screen previously told you it was
blocked while offering nothing to do about it. There is now a "Raise the cap" action on any bot at
or near its budget, offered as fixed steps (+$5, +$25, double) rather than a text field — this is
a number typed under time pressure on a phone keyboard, where a slipped decimal is a real bill.
It says honestly that the refused turn is **not** retried: raising the cap lets the next one run.

**Chat — kept, and taught the three events it was dropping.** `takeover`, `desktop` and the
delegation half of `handoff` all arrived on the `unknown` arm and were silently ignored:

- A **takeover mid-conversation** showed nothing at all. The bot asked for help, in the place the
  person was looking, and the app swallowed it. There is now an inline card, and a fallback that
  recovers the request from the `done` frame's `awaiting_human` flag for a client that
  reconnected mid-turn and missed the event.
- A **cold-starting desktop** takes 30–90 seconds, during which the turn looked hung — the single
  most common way an agent product reads as broken. Progress is rendered, with the elapsed count.
- A **delegation** now reads "Delegated · person → lead_generator → sales" rather than "Handed off
  to Sales". That is the difference between showing a team working and showing a topic change.
- `cost` is parsed and deliberately **not** rendered per step: a line of spend after every model
  call buries the conversation. Spend is read on the Usage tab.

**Routines, connectors/MCP, the bot builder, memory/KB — omitted as a decision.** They are
authoring surfaces: long forms, credentials, JSON, recording a multi-step flow. Nobody wants to
register an MCP server on a phone, and a half-usable version costs real time while making the
product feel worse. If a routine needs approval to _run_, that approval still reaches the phone,
which is the time-sensitive part.

**Rehearsal, plans and `action-log` undo — left, and this is the closest call.** Undo is a
decision, not authoring, so by this lane's own rule it has a claim on the phone. It was left out
because it is the decision that most needs context: which of forty executed effects to reverse,
whether an inverse honestly exists, and what `reversible=False` means for the rest. Getting that
wrong from a phone is worse than not offering it. It is the first thing to add if someone wants a
sixth surface — and it would displace Work, not Inbox.

### Where mobile is deliberately ahead

- **Push notifications** deep-linking to the exact item.
- **Biometric unlock** of the stored session token.
- **A runtime API-base-URL override** in Settings, which the desktop bakes in at build time.
- **Raise a budget cap** in two taps.
- **The work-item ledger**, which has no desktop UI yet.

### Type scale: one migration, deliberately

The previous lane declined to mechanically rewrite ~75 literal `fontSize` values onto the shared
scale, because the sizes do not map one to one and it could not see the screens. That judgement
still holds and this lane did not overturn it — there is still no simulator on this machine.

What changed:

- **New screens use the scale** where the choice is fresh rather than a change to an existing
  look: `type.labelCaps` for section headings and eyebrows, `type.eyebrow` for the brand line,
  `type.label` for filter chips.
- **One existing site migrated**: the home eyebrow, from a hand-rolled
  `{fontSize: 11, letterSpacing: 2, fontWeight: 700}` to `type.eyebrow`
  (`11 / +1.8 / 700`, uppercase). Same size, same weight, tracking within a fifth of a pixel — and
  now it moves when the brand does. `tracking.widest` is measured off the Nesqual logo's tagline,
  which is what makes an eyebrow read as Nesqual.

The other four `letterSpacing` sites and the remaining `fontSize` literals are still waiting on
someone with a simulator open. That is a real gap, stated rather than papered over.

---

## Part 3 — Verification, and what it does not cover

`tsc` and a successful bundle prove a render bug cannot ship. They prove nothing about a
**decision** bug, and every interesting function in this lane decides something a person acts on.
So `apps/mobile/src/lib/__checks__/smoke.mjs` (`npm run check`) asserts the ones that have exactly
one right answer with no screen involved — 69 checks, against the **real source**, not a copy.

Loading the real source needed two small design changes, both improvements in their own right:
`src/lib/desktopStream.ts` takes the API base URL as an argument instead of importing the client,
and `notificationTarget` was split out of `src/notifications/index.ts` into
`src/notifications/target.ts`. Both modules now import nothing native, so Node's type stripping
can load them. A hand-copy in a test is a test of the hand-copy.

What is asserted, and why each one matters:

| Check                                | The failure it catches                                             |
| ------------------------------------ | ------------------------------------------------------------------ |
| `approvalExecutionOutcome`           | A rejection rendered as "execution failed" — the live bug          |
| `takeoverFromRun`                    | A Continue button on a run the API will 409 as `run_not_resumable` |
| `takeoverFromEvent`                  | A `resumed` frame minting a fresh card for a task already moving   |
| `takeoverFromDone`                   | A takeover swallowed because the client reconnected mid-turn       |
| `notificationTarget`                 | A notification tap that opens the wrong screen, or nothing         |
| `desktopStreamUrl`                   | A viewer URL a phone cannot route to — the live bug                |
| `parseSseEvent` / `parseThreadEvent` | New arms regressing, or forward compatibility breaking             |

**What none of this covers, stated plainly:** no layout, no colour, no gesture, no Dynamic Type
behaviour, no VoiceOver reading order, no thumb reachability in the hand. There is no iOS
simulator on this machine and none of the new screens has been _seen_. Safe areas, touch targets
(44pt), `accessibilityLabel`s, `accessibilityLiveRegion` on the two counters and
`allowFontScaling={false}` on the tab glyphs are all written to the guidelines — but written to
them is not the same as verified against them, and the first person with a Mac should assume the
new screens need a visual pass.

---

## Part 4 — Outstanding

Ordered by value:

1. **Look at it.** Every screen in this lane is unseen. One session with a simulator would confirm
   the layouts and let someone finish the type-scale migration in the same sitting.
2. **A takeover push.** The one asymmetry left in the product's best story: an approval buzzes the
   phone, a bot stuck on a login does not. It is a small addition to
   `apps/api/app/services/notifications.py`, which this lane does not own.
3. **Apple Developer Program** — \$99/year, and the D-U-N-S lead time is the real cost. A push
   token needs a build with APNs credentials; the EAS project id (now linked) is only half of it.
   See `docs/mobile-release.md`.
4. **Bundle Inter + Poppins via `expo-font`** to close the typeface gap.
5. **A desktop UI for work items**, so the ledger is not phone-only.
6. **`action-log` undo**, if a sixth surface is wanted. See the reasoning above.
7. **Keep `packages/protocol` the only home for wire shapes.** This lane moved four shapes out of
   `apps/desktop/src/types.ts` and found a real bug in a fifth. The parity check
   (`npm run check:api --workspace @nesqbot/protocol`) guards the API↔TS edge; nothing yet guards
   TS↔app-local re-declaration except review.
