/**
 * `lib/mentions.ts` against the cases that define the rule server-side.
 *
 * `mentionedBotIds` is a copy of `orchestrator.mentioned_bots`
 * (`apps/api/app/services/orchestrator.py`), and a copy drifts. When it does,
 * the app writes one teammate's name into the message text and sends a
 * different one in `mention_bot_ids` — the two channels disagree, one bot is
 * seated and another answers, and nothing anywhere reports an error.
 *
 * These are the same cases as section 4 of
 * `apps/api/tests/services/test_mentions_seat_bots.py`, deliberately worded the
 * same so the pair can be read side by side. Node runs the TypeScript directly
 * (type stripping), so this needs no test runner and no dependency — the
 * desktop app has neither.
 *
 * Run: `npm run check:mentions` (part of `npm run lint`).
 */

import assert from "node:assert/strict"

// Imported by URL, not by path: this repo lives on a mapped network drive and
// `import("n:\\...")` is not a scheme the ESM loader accepts.
const { mentionedBotIds, mentionQuery, matchCandidates, applyMention } = await import(
  new URL("../src/lib/mentions.ts", import.meta.url).href
)

const bot = (name, slug) => ({ id: slug, name, slug })

const ROSTER = [
  bot("Chief of Staff", "chief_of_staff"),
  bot("Lead Generator", "lead_generator"),
  bot("Sales", "sales"),
  bot("Ops", "ops"),
  bot("Support", "support"),
]

const slugs = (text, roster = ROSTER) => mentionedBotIds(text, roster)

const tests = {
  "the message the CEO actually sent"() {
    assert.deepEqual(
      slugs(
        "I need you to work with @Lead Generator to get leads, @Sales to close deals " +
          "inside our platform.",
      ),
      ["lead_generator", "sales"],
    )
  },

  "every spelling of a handle is one mention"() {
    for (const text of ["@Lead Generator", "@lead_generator", "@lead-generator", "@LEAD GENERATOR"]) {
      assert.deepEqual(slugs(text), ["lead_generator"], text)
    }
  },

  "a longer name wins over a shorter one inside it"() {
    const roster = [...ROSTER, bot("Lead", "lead")]
    assert.deepEqual(slugs("@Lead Generator go", roster), ["lead_generator"])
  },

  "what is not a mention"() {
    assert.deepEqual(slugs("no mentions at all"), [])
    assert.deepEqual(slugs(""), [])
    assert.deepEqual(slugs("forwarded from @someone.else — reply to rita@acme.test"), [])
    assert.deepEqual(slugs("sales should probably close these"), [])
  },

  "a two letter name cannot match inside a word"() {
    assert.deepEqual(slugs("email me @bobbyexample.com", [bot("Bo", "bo")]), [])
  },

  // ---- the picker, which has no server-side counterpart --------------------

  "the picker opens on an at sign that starts a word"() {
    assert.deepEqual(mentionQuery("@sal", 4), { start: 0, query: "sal" })
    assert.deepEqual(mentionQuery("tell @sal", 9), { start: 5, query: "sal" })
    // An email address is not somebody reaching for a teammate.
    assert.equal(mentionQuery("rita@acme.test", 14), null)
    // Nor is a paragraph that happened to contain an `@` several words back.
    assert.equal(mentionQuery("@ok so then we should", 21), null)
    assert.equal(mentionQuery("@sales\nnext line", 16), null)
  },

  "a prefix beats a substring"() {
    const roster = [bot("Sales", "sales"), bot("Upsale Desk", "upsale_desk")]
    assert.deepEqual(
      matchCandidates("sal", roster).map((b) => b.slug),
      ["sales", "upsale_desk"],
    )
  },

  "an empty query offers everyone in order"() {
    assert.deepEqual(
      matchCandidates("", ROSTER, 3).map((b) => b.slug),
      ["chief_of_staff", "lead_generator", "sales"],
    )
  },

  "choosing writes the handle the parser will read back"() {
    const text = "tell @sal about it"
    const query = mentionQuery(text, 9)
    const next = applyMention(text, query, 9, bot("Sales", "sales"))
    assert.equal(next.text, "tell @Sales about it")
    assert.equal(next.caret, "tell @Sales ".length)
    // The whole point: what was inserted resolves to the bot that was picked.
    assert.deepEqual(mentionedBotIds(next.text, ROSTER), ["sales"])
  },
}

let failed = 0
for (const [name, run] of Object.entries(tests)) {
  try {
    run()
  } catch (err) {
    failed += 1
    console.error(`check-mentions: FAIL — ${name}\n  ${err.message}`)
  }
}

if (failed) process.exit(1)
console.log(`check-mentions: ${Object.keys(tests).length} cases pass`)
