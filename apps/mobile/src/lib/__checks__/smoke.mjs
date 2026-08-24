/**
 * Runnable checks for the logic that decides what the person is shown.
 *
 *     npm run check          (from apps/mobile)
 *     node src/lib/__checks__/smoke.mjs
 *
 * WHY THIS EXISTS
 * ---------------
 * This app has no test runner, and the machine it is built on has no iOS
 * simulator, so "it typechecks and it bundles" was the whole verification
 * story. That is adequate for a render bug and useless for a *decision* bug —
 * and every function checked here decides something a person then acts on:
 *
 *   - whether a rejected approval renders as "execution failed" (it did);
 *   - whether a parked run offers a Continue button that can actually work;
 *   - whether a notification tap opens the right screen, or nothing;
 *   - whether the desktop viewer builds a URL a phone can reach (it did not).
 *
 * Each has exactly one right answer that can be asserted with no screen, so it
 * is asserted. This is **not** a substitute for looking at the app: nothing
 * here proves a layout, a colour, a gesture or a font size.
 *
 * HOW IT LOADS THE REAL SOURCE
 * ----------------------------
 * Node ≥22 strips TypeScript types natively, but only resolves `.ts` as ESM
 * when the nearest `package.json` says `"type": "module"` — and `apps/mobile`
 * must not, because Metro and the Expo config loader depend on it not saying
 * so. So each module under test is copied byte-for-byte into a temporary
 * directory that does carry that flag, and imported from there.
 *
 * The consequence worth knowing: the modules checked here are the ones that
 * import **nothing native**. `src/lib/takeover.ts`, `src/lib/desktopStream.ts`
 * and `src/notifications/target.ts` were each shaped that way deliberately —
 * `desktopStream` takes the API base URL as an argument instead of reading it
 * from the client, and `notificationTarget` was split out of
 * `src/notifications/index.ts` — precisely so this file can load them rather
 * than re-implement them. A hand-copy in a test is a test of the hand-copy.
 */
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const mobileRoot = resolve(here, "..", "..", "..")
const repoRoot = resolve(mobileRoot, "..", "..")

const staging = mkdtempSync(join(tmpdir(), "nesqbot-checks-"))
writeFileSync(join(staging, "package.json"), JSON.stringify({ type: "module" }), "utf8")

/** Copy one real source file into the ESM staging dir and import it. */
async function load(relativeTo, relativePath, as) {
  const target = join(staging, as)
  cpSync(join(relativeTo, relativePath), target)
  return import(pathToFileURL(target).href)
}

let failures = 0
let checks = 0

function check(name, actual, expected) {
  checks += 1
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) {
    failures += 1
    console.error(`  FAIL  ${name}\n        expected ${e}\n        got      ${a}`)
  }
}

function group(name, fn) {
  console.log(name)
  fn()
}

try {
  /* ================================================================ *
   * packages/protocol — the approval execution union
   * ================================================================ */
  // entities.ts imports only types from ./core, so stripping leaves nothing to
  // resolve and it loads standalone.
  const entities = await load(repoRoot, "packages/protocol/src/entities.ts", "entities.ts")
  const { approvalExecutionOutcome } = entities

  group("approvalExecutionOutcome — three arms, not two", () => {
    check("approved and ran", approvalExecutionOutcome({ ok: true, result: { id: 1 } }), "ran")
    check("approved, refused at execution", approvalExecutionOutcome({ ok: false, error: "element gone" }), "failed")
    // The regression this helper exists for. A *rejection* that resumed a run
    // comes back as `{continuation: …}` with no `ok` at all, and the old
    // `execution.ok ? "OK" : "FAILED"` rendered an ordinary rejection as an
    // execution failure.
    check(
      "rejected but the task carried on",
      approvalExecutionOutcome({ continuation: { continued: true, run_id: "r1" } }),
      "not-executed",
    )
    check("no execution envelope", approvalExecutionOutcome(null), "not-executed")
    check("undefined", approvalExecutionOutcome(undefined), "not-executed")
  })

  /* ================================================================ *
   * packages/protocol — done frames and run states
   * ================================================================ */
  const events = await load(repoRoot, "packages/protocol/src/events.ts", "events.ts")
  const { isStreamClosedDone, isTakeoverRequested, parseThreadEvent, parseSseEvent } = events
  const core = await load(repoRoot, "packages/protocol/src/core.ts", "core.ts")
  const { isParkedRunStatus, isTerminalRunStatus, isRunStatus } = core

  group("done frames — two shapes, one name", () => {
    check("close-out", isStreamClosedDone({ thread_id: "t1", reason: "closed" }), true)
    check("finished turn", isStreamClosedDone({ message_id: "m1", bot_id: "b1" }), false)
    // A parked turn's `done` carries a real message and the run it parked on.
    // Treating it as a close-out would swallow the bot's own "I need you at the
    // screen" reply.
    check(
      "parked turn is not a close-out",
      isStreamClosedDone({ message_id: "m1", run_id: "r1", awaiting_human: true }),
      false,
    )
  })

  group("run statuses — awaiting_human is a first-class state", () => {
    check("recognised", isRunStatus("awaiting_human"), true)
    check("parked, not finished", isParkedRunStatus("awaiting_human"), true)
    check("so is awaiting_approval", isParkedRunStatus("awaiting_approval"), true)
    check("running is not parked", isParkedRunStatus("running"), false)
    check("and not terminal", isTerminalRunStatus("awaiting_human"), false)
  })

  group("event parsing — the new arms come back typed, not as `unknown`", () => {
    // Before this lane these three routed to the `unknown` arm and every client
    // had to sniff the raw name.
    const takeover = parseThreadEvent(
      "takeover",
      JSON.stringify({ phase: "requested", run_id: "r1", reason: "LinkedIn wants a password" }),
    )
    check("takeover is a real arm", takeover.event, "takeover")
    check("and it is a request", isTakeoverRequested(takeover.data), true)

    const desktop = parseSseEvent("desktop", JSON.stringify({ bot_id: "b1", phase: "starting", elapsed_seconds: 45 }))
    check("desktop is a real arm", desktop.event, "desktop")

    const handoff = parseSseEvent(
      "handoff",
      JSON.stringify({
        bot_id: "b2",
        bot_name: "Sales",
        from_bot_name: "Lead Gen",
        delegated: true,
        chain: "person → lead_generator → sales",
      }),
    )
    check("handoff keeps the delegation keys", handoff.data.delegated, true)
    check("and the chain", handoff.data.chain, "person → lead_generator → sales")

    // Forward compatibility must survive: a name this build does not know still
    // routes to `unknown` rather than throwing.
    check("an unknown name still routes", parseSseEvent("something_new", "{}").event, "unknown")
    // A takeover with no run_id is unusable — resume is addressed by run id.
    check(
      "takeover without a run id is not accepted",
      parseThreadEvent("takeover", '{"phase":"requested"}').event,
      "unknown",
    )
    // Bad JSON must not throw.
    check("bad JSON routes to unknown", parseSseEvent("done", "not json at all").event, "unknown")
  })

  /* ================================================================ *
   * src/lib/takeover.ts
   * ================================================================ */
  const takeoverLib = await load(mobileRoot, "src/lib/takeover.ts", "takeover.ts")
  const { takeoverFromRun, takeoverFromEvent, takeoverFromDone, mergeTakeovers } = takeoverLib

  group("takeoverFromRun — only offers a button that can work", () => {
    const parked = {
      id: "run-1",
      status: "awaiting_human",
      bot_id: "bot-1",
      thread_id: "thread-1",
      created_at: "2026-08-24T00:00:00Z",
      detail: {
        agent: {
          resume_count: 2,
          goal: "Book the demo",
          takeover: {
            reason: "LinkedIn is asking for a password",
            what_you_need: "Sign in, then press continue",
            asked_at: "2026-08-24T01:00:00Z",
          },
        },
      },
    }
    check("reads the bot's own words", takeoverFromRun(parked).reason, "LinkedIn is asking for a password")
    check("and the instruction", takeoverFromRun(parked).whatYouNeed, "Sign in, then press continue")
    check("carries the resume count", takeoverFromRun(parked).resumeCount, 2)
    check("and the goal", takeoverFromRun(parked).goal, "Book the demo")
    check("marked as restored, not live", takeoverFromRun(parked).source, "parked")

    // A run whose status says `awaiting_human` but which carries no agent state
    // is NOT resumable — the API answers 409 `run_not_resumable`. Offering a
    // Continue button for it would be offering a button that cannot work.
    check("no agent state means no card", takeoverFromRun({ id: "r", status: "awaiting_human", detail: {} }), null)
    check("no detail at all", takeoverFromRun({ id: "r", status: "awaiting_human", detail: null }), null)
    check(
      "a running run is not a takeover",
      takeoverFromRun({ id: "r", status: "running", detail: { agent: { takeover: {} } } }),
      null,
    )
    // Parked with no reason recorded: still a real request, generic words, no
    // invention.
    check(
      "falls back rather than inventing a reason",
      takeoverFromRun({ id: "r", status: "awaiting_human", detail: { agent: {} } }).reason,
      "This task is waiting for you.",
    )
  })

  group("takeoverFromEvent — only `requested` raises one", () => {
    const raised = takeoverFromEvent({ phase: "requested", run_id: "r1", reason: "MFA code needed" })
    check("a request", raised.runId, "r1")
    check("keeps the reason", raised.reason, "MFA code needed")
    check("marked live", raised.source, "live")
    // `resumed` releases a request. Minting a fresh card from it would put the
    // person back on a screen for a task that is already moving again.
    check("resumed does not raise", takeoverFromEvent({ phase: "resumed", run_id: "r1" }), null)
    check("an unknown phase does not raise", takeoverFromEvent({ phase: "something_new", run_id: "r1" }), null)
    check("no run id, no card", takeoverFromEvent({ phase: "requested" }), null)
  })

  group("takeoverFromDone — the reconnected-mid-turn safety net", () => {
    check(
      "recovers from the flag alone",
      takeoverFromDone({ message_id: "m1", run_id: "r9", awaiting_human: true }, "Karl").runId,
      "r9",
    )
    check(
      "takes the bot name it is given",
      takeoverFromDone({ run_id: "r9", awaiting_human: true }, "Karl").botName,
      "Karl",
    )
    check("an ordinary finished turn is not one", takeoverFromDone({ message_id: "m1", run_id: "r9" }), null)
    check("nor a close-out", takeoverFromDone({ thread_id: "t1", reason: "closed" }), null)
    check("awaiting_human with no run id is unusable", takeoverFromDone({ awaiting_human: true }), null)
  })

  group("mergeTakeovers — one card per run, newest first", () => {
    const a = { runId: "r1", askedAt: "2026-08-24T01:00:00Z", botName: "Karl", source: "live" }
    const b = { runId: "r2", askedAt: "2026-08-24T02:00:00Z", botName: null, source: "parked" }
    const merged = mergeTakeovers([a], [b])
    check("both kept", merged.length, 2)
    check("newest first", merged[0].runId, "r2")
    // The same run arriving twice — live, then again from the poll — must be one
    // row, and the name the live frame carried must not be lost to the poll.
    const again = mergeTakeovers(
      [a],
      [{ runId: "r1", askedAt: "2026-08-24T01:05:00Z", botName: null, source: "parked" }],
    )
    check("deduplicated on run id", again.length, 1)
    check("keeps the name the poll does not carry", again[0].botName, "Karl")
  })

  /* ================================================================ *
   * src/notifications/target.ts
   * ================================================================ */
  const targetMod = await load(mobileRoot, "src/notifications/target.ts", "target.ts")
  const { notificationTarget } = targetMod

  group("notificationTarget — a tap must open something", () => {
    // The exact payload `apps/api/app/services/notifications.py` sends today.
    check(
      "the live API payload",
      notificationTarget({ approval_id: "a1", bot_id: "b1", risk: "spend", kind: "connector_action" }),
      { kind: "approval", id: "a1" },
    )
    check("a deep link", notificationTarget({ url: "nesqbot://approvals/a2" }), { kind: "approval", id: "a2" })
    check("an explicit takeover", notificationTarget({ kind: "takeover", run_id: "r1" }), {
      kind: "takeover",
      runId: "r1",
    })
    // A run that parked on an approval carries both ids. The approval is the
    // decision to make, so it wins unless the payload says otherwise.
    check("both ids, no explicit kind", notificationTarget({ approval_id: "a3", run_id: "r3" }), {
      kind: "approval",
      id: "a3",
    })
    check(
      "explicit kind beats an incidental id",
      notificationTarget({ kind: "takeover", run_id: "r5", approval_id: "a5" }),
      {
        kind: "takeover",
        runId: "r5",
      },
    )
    check("a bare run id", notificationTarget({ run_id: "r4" }), { kind: "takeover", runId: "r4" })
    // Never nothing.
    check("garbage", notificationTarget({ hello: "world" }), { kind: "inbox" })
    check("null", notificationTarget(null), { kind: "inbox" })
    check("a string", notificationTarget("nope"), { kind: "inbox" })
    check("empty strings are not ids", notificationTarget({ approval_id: "   " }), { kind: "inbox" })
  })

  /* ================================================================ *
   * src/lib/desktopStream.ts
   * ================================================================ */
  const streamLib = await load(mobileRoot, "src/lib/desktopStream.ts", "desktopStream.ts")
  const { desktopStreamUrl, websockifyPath, apiOrigin, ticketTtlMs } = streamLib

  group("desktopStreamUrl — the phone must be able to reach it", () => {
    const ticket = {
      ticket: "v1:bot:user:123:abc",
      expires_at: "2026-08-24T01:01:00Z",
      expires_in: 60,
      stream_path: "/bots/b1/desktop/stream/v1abc/vnc.html",
      ws_path: "/bots/b1/desktop/stream/v1abc/websockify",
      vnc_password: "pw",
    }
    const prod = "https://api.example.test/api"

    check("origin is recovered", apiOrigin(prod), "https://api.example.test")
    // The mount prefix has to be put back on: the ticket's paths are relative to
    // the API root, not the host, so a bare `ws_path` would point at `/bots/…`
    // and miss `/api` entirely.
    check(
      "websockify keeps the /api prefix",
      websockifyPath(ticket, prod),
      "api/bots/b1/desktop/stream/v1abc/websockify",
    )
    // …and must have NO leading slash: noVNC builds `ws://host/<path>`.
    check("no leading slash", websockifyPath(ticket, prod).startsWith("/"), false)

    const url = desktopStreamUrl(ticket, { base: prod })
    check("absolute, on the API origin", url.startsWith("https://api.example.test/api/bots/b1/"), true)
    // The bug this replaced: the old screen used `BotDesktop.stream_url`, a
    // 10.60.x.x VNet address no phone can route to, so the live view never
    // worked once.
    check("never a private VNet address", url.includes("10.60."), false)
    check("view-only by default", url.includes("view_only=1"), true)
    check("no reconnect on a burned ticket", url.includes("reconnect=0"), true)
    check("scales rather than resizing the bot's screen", url.includes("resize=scale"), true)
    check(
      "interactive drops view_only",
      desktopStreamUrl(ticket, { base: prod, viewOnly: false }).includes("view_only"),
      false,
    )

    // A base with no path segment — the shape a LAN dev server takes — must not
    // produce a doubled or missing slash.
    const bare = "http://192.168.1.10:8080"
    check("no prefix to restore", websockifyPath(ticket, bare), "bots/b1/desktop/stream/v1abc/websockify")
    check("still absolute", desktopStreamUrl(ticket, { base: bare }).startsWith("http://192.168.1.10:8080/bots/"), true)
    check("origin of a bare base is itself", apiOrigin(bare), bare)

    // Prefer the server's own countdown over the phone's clock, which may be
    // minutes out either way.
    check("ttl from expires_in", ticketTtlMs(ticket), 60000)
    check("ttl never negative", ticketTtlMs({ ...ticket, expires_in: -5 }), 0)
  })
} finally {
  rmSync(staging, { recursive: true, force: true })
}

console.log("")
if (failures > 0) {
  console.error(`FAIL — ${failures} of ${checks} checks failed`)
  process.exit(1)
}
console.log(`ok — ${checks} checks passed against the real source`)
