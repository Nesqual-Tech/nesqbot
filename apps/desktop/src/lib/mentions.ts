/**
 * `@` mentions in the composer.
 *
 * The composer advertised "@ to mention a teammate — that chooses who answers"
 * and nothing in this app implemented it: `Composer` was a plain textarea, and
 * `ChatPane` called `messages.send(text)` with no second argument, so
 * `mention_bot_ids` was permanently `undefined` on the wire. The server-side
 * half was complete the whole time.
 *
 * Two channels reach the API and they answer different questions:
 *
 * * the **text** — `orchestrator.mentioned_bots` reads it and *seats* whoever
 *   it names, so a teammate who was not in the room joins it;
 * * **`mention_bot_ids`** — decides who *answers* this turn.
 *
 * Only the second one was missing, which is why typing `@Sales` at a chief of
 * staff appeared to do nothing useful: Sales quietly joined the thread and the
 * chief answered anyway.
 *
 * `mentionedBotIds` mirrors `orchestrator.mentioned_bots`
 * (`apps/api/app/services/orchestrator.py`) — that function is the authority,
 * this is a copy, and they have to agree or the app will name one bot in the
 * text and a different one in the ids. Derived from the final text rather than
 * remembered from the picker on purpose: a handle the person then deleted is
 * not a mention, and someone who types `@sales` straight through without
 * touching the popup means it exactly as much as someone who clicked.
 */

/** Structurally a `Bot`, narrowed to what a mention needs — which is also what
 *  `BotAvatar` needs, so the picker can draw one. */
export interface MentionCandidate {
  id: string
  name: string
  slug: string
}

/** `ADDRESSED_BOT_MIN_TOKEN_CHARS` in the orchestrator. Keeps a two-letter name
 *  from matching inside an ordinary word. */
const MIN_HANDLE_CHARS = 3

/** Names have spaces, so `@Lead Generator`, `@lead_generator` and
 *  `@lead-generator` are one handle. */
const SEPARATOR = "[\\s_-]*"

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

function handlePattern(handle: string): RegExp {
  const body = handle.split(/\s+/).map(escapeRegExp).join(SEPARATOR)
  return new RegExp(`@\\s*${body}`, "i")
}

/**
 * The bots an `@` mention in `text` resolves to, longest name first.
 *
 * Longest first, and each match blanked as it is claimed, so `@Lead Generator`
 * cannot also count as a mention of a bot called `Lead` — the prefix is a
 * legitimate match for the shorter handle, but the person named one bot.
 * Blanked rather than removed so later matches still see the original offsets.
 */
export function mentionedBotIds(text: string, candidates: MentionCandidate[]): string[] {
  if (!text.includes("@")) return []
  let remaining = text
  const found: string[] = []
  const byLongestName = [...candidates].sort((a, b) => (b.name?.length ?? 0) - (a.name?.length ?? 0))
  for (const bot of byLongestName) {
    for (const raw of [bot.name ?? "", bot.slug ?? ""]) {
      const handle = raw.trim()
      if (handle.length < MIN_HANDLE_CHARS) continue
      const hit = handlePattern(handle).exec(remaining)
      if (!hit) continue
      found.push(bot.id)
      remaining =
        remaining.slice(0, hit.index) +
        " ".repeat(hit[0].length) +
        remaining.slice(hit.index + hit[0].length)
      break
    }
  }
  return found
}

export interface MentionQuery {
  /** Index of the `@` that opened it. */
  start: number
  /** What has been typed after the `@`, verbatim. */
  query: string
}

/**
 * The mention being typed at `caret`, or `null`.
 *
 * Opens on an `@` that starts a word — an email address or a `you@` mid-word is
 * not a mention. Closes on a newline, and after three words, because the
 * longest real handle is a display name and a paragraph is prose the person is
 * writing, not a name they are still spelling.
 */
export function mentionQuery(text: string, caret: number): MentionQuery | null {
  const before = text.slice(0, caret)
  const at = before.lastIndexOf("@")
  if (at < 0) return null
  const preceding = at > 0 ? before[at - 1] : ""
  if (preceding && !/[\s(]/.test(preceding)) return null
  const query = before.slice(at + 1)
  if (/[\n\r]/.test(query)) return null
  if (query.split(/\s+/).length > 3) return null
  return { start: at, query }
}

/** Candidates worth offering for `query`, best first. */
export function matchCandidates(
  query: string,
  candidates: MentionCandidate[],
  limit = 6,
): MentionCandidate[] {
  const needle = query.trim().toLowerCase().replace(/[\s_-]/g, "")
  if (!needle) return candidates.slice(0, limit)
  const scored: { bot: MentionCandidate; rank: number }[] = []
  for (const bot of candidates) {
    const handles = [bot.name ?? "", bot.slug ?? ""].map((h) =>
      h.toLowerCase().replace(/[\s_-]/g, ""),
    )
    // A prefix beats a contains, so typing "sa" puts Sales above a bot whose
    // name merely has "sa" in the middle of it.
    const rank = handles.some((h) => h.startsWith(needle))
      ? 0
      : handles.some((h) => h.includes(needle))
        ? 1
        : -1
    if (rank >= 0) scored.push({ bot, rank })
  }
  scored.sort((a, b) => a.rank - b.rank || a.bot.name.localeCompare(b.bot.name))
  return scored.slice(0, limit).map((s) => s.bot)
}

/**
 * `text` with the mention at `query` completed to `bot`, and where the caret
 * goes afterwards.
 *
 * The display name is written in, not the slug: it is what the person picked
 * out of the list, it reads as a sentence, and `mentionedBotIds` matches either.
 * One trailing space so the next word is not glued to the handle.
 */
export function applyMention(
  text: string,
  query: MentionQuery,
  caret: number,
  bot: MentionCandidate,
): { text: string; caret: number } {
  const handle = `@${bot.name} `
  // The handle brings its own trailing space, so swallow one that is already
  // there — completing mid-sentence should not leave a double gap behind.
  const rest = text.slice(caret)
  const next = text.slice(0, query.start) + handle + (rest.startsWith(" ") ? rest.slice(1) : rest)
  return { text: next, caret: query.start + handle.length }
}
