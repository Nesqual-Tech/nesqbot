/**
 * Markdown for text this app does not trust.
 *
 * Everything rendered through here is model output, and a good deal of it is
 * model output that quotes a web page a bot was reading. Treat it as hostile.
 *
 * ## Why this is hand-written and not a dependency
 *
 * The interesting property of this module is not what it parses, it is what it
 * *cannot* emit. `parseMarkdown` returns a typed node tree; `components/
 * Markdown.tsx` turns that tree into React elements. There is no HTML string
 * anywhere in the pipeline and therefore no `dangerouslySetInnerHTML` at the
 * end of it, so "raw HTML is disabled" is not a configuration flag somebody can
 * flip back on by accident — it is the absence of a code path. `<script>` in
 * the source text can only ever come out the other side as a `text` node, which
 * React escapes.
 *
 * The alternative was `marked` + `DOMPurify` (~21 KB gzipped between them), or
 * `react-markdown` + `remark-gfm` + `rehype-sanitize` (~45 KB gzipped). Both
 * are good libraries and both are more complete than this file. Both also parse
 * to HTML and then rely on a sanitiser staying correctly configured, in a
 * webview that holds a production session token and can invoke Rust commands.
 * That is a bigger blast radius than "an unusual list nests wrongly", which is
 * the worst this file can do.
 *
 * ## What it covers
 *
 * Blocks: ATX headings, fenced code, thematic breaks, blockquotes, ordered and
 * unordered lists (nested, tight/loose), GFM tables, paragraphs.
 * Inline: code spans, strong, emphasis, strikethrough, links (inline, angle
 * autolinks, bare URLs), images-as-links, hard and soft breaks, backslash
 * escapes.
 *
 * Deliberately absent: raw HTML (see above), reference links and definitions,
 * footnotes, indented code blocks, setext headings, task-list checkboxes.
 * Anything unsupported degrades to literal text, never to an error.
 *
 * ## Streaming
 *
 * The parser is fed partial input on every token. Two consequences are handled
 * on purpose: an unterminated fence produces a `code` block with `open: true`
 * rather than a run of stray paragraphs (so a half-arrived code block does not
 * reflow the bubble on every token), and an unmatched inline delimiter falls
 * back to literal text. One linear pass, no backtracking across the whole
 * string, and no regex with nested quantifiers pointed at untrusted input.
 */

/* ------------------------------------------------------------------ *
 * Nodes
 * ------------------------------------------------------------------ */

export type InlineNode =
  | { type: "text"; value: string }
  | { type: "break" }
  | { type: "code"; value: string }
  | { type: "strong"; children: InlineNode[] }
  | { type: "em"; children: InlineNode[] }
  | { type: "del"; children: InlineNode[] }
  | { type: "link"; href: string | null; title?: string; image?: boolean; children: InlineNode[] }

export type TableAlign = "left" | "center" | "right" | null

export type BlockNode =
  | { type: "paragraph"; children: InlineNode[] }
  | { type: "heading"; level: number; children: InlineNode[] }
  | { type: "code"; lang: string | null; value: string; open: boolean }
  | { type: "list"; ordered: boolean; start: number; tight: boolean; items: BlockNode[][] }
  | { type: "quote"; children: BlockNode[] }
  | { type: "table"; align: TableAlign[]; head: InlineNode[][]; rows: InlineNode[][][] }
  | { type: "hr" }

/* ------------------------------------------------------------------ *
 * Link safety
 * ------------------------------------------------------------------ */

/**
 * The only schemes a model-produced link is allowed to carry.
 *
 * `javascript:` and `data:` are the two that matter — the first is script
 * execution inside a privileged webview, the second smuggles a whole document
 * past a check that only looked at the first word. Neither is on this list, and
 * nothing that is not on this list ever becomes an `href`.
 */
const SAFE_SCHEMES = new Set(["http", "https", "mailto"])

/**
 * A small named-entity table. The ones that appear in real prose, plus the
 * handful (`&colon;`, `&sol;`, `&NewLine;`, `&Tab;`) that exist mostly because
 * they are the classic way to hide a scheme from a naive check.
 */
const NAMED_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: "\u00a0",
  colon: ":",
  sol: "/",
  bsol: "\\",
  newline: "\n",
  tab: "\t",
  lpar: "(",
  rpar: ")",
  period: ".",
  comma: ",",
  num: "#",
  excl: "!",
  quest: "?",
  semi: ";",
  equals: "=",
  hellip: "…",
  mdash: "—",
  ndash: "–",
  rsquo: "’",
  lsquo: "‘",
  ldquo: "“",
  rdquo: "”",
}

const ENTITY_RE = /&(#[xX][0-9a-fA-F]{1,6}|#\d{1,7}|[a-zA-Z][a-zA-Z0-9]{1,31});/g

/**
 * Resolve HTML entities.
 *
 * Runs exactly once, and its output never re-enters an HTML parser: React sets
 * `href` as a DOM property, so there is no second decoding pass for a
 * double-encoded payload (`&amp;#x6a;avascript:`) to survive into. That payload
 * decodes once to `&#x6a;avascript:`, which has no valid scheme and is
 * therefore refused by `safeHref` rather than half-decoded into one.
 */
export function decodeEntities(input: string): string {
  if (!input.includes("&")) return input
  return input.replace(ENTITY_RE, (whole: string, body: string) => {
    if (body.charCodeAt(0) === 35 /* # */) {
      const hex = body[1] === "x" || body[1] === "X"
      const code = Number.parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10)
      if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff) return whole
      // Lone surrogates are not characters; leaving them literal is safer than
      // emitting one for later string maths to misread.
      if (code >= 0xd800 && code <= 0xdfff) return whole
      try {
        return String.fromCodePoint(code)
      } catch {
        return whole
      }
    }
    const exact = NAMED_ENTITIES[body]
    if (exact !== undefined) return exact
    const lowered = NAMED_ENTITIES[body.toLowerCase()]
    return lowered !== undefined ? lowered : whole
  })
}

/** Control characters, every flavour of space, and the Unicode format
 *  characters. None of them belong in a URL, and all of them have been used to
 *  break `javascript:` into something a substring check does not recognise. */
const URL_NOISE_RE =
  /[\u0000-\u0020\u007f-\u00a0\u00ad\u034f\u061c\u180e\u2000-\u200f\u2028-\u202f\u205f-\u206f\u3000\ufeff\ufff9-\ufffb]/g

/**
 * Everything a URL must survive before it is allowed to be an `href`.
 *
 * Order matters. Entities are resolved first, because `java&#x09;script:` is
 * not `javascript:` until they are. The noise above is then removed, because
 * `java\tscript:` is not `javascript:` until it is either. Only then is the
 * scheme read, and only then is it checked against the allowlist.
 *
 * A URL with *no* scheme is refused too: `//evil.example` is protocol-relative
 * and `/settings` is a navigation inside the app, and neither is something a
 * model should be able to hand a person as a clickable target.
 *
 * Returns the cleaned URL, or `null` — and `null` renders as inert text.
 */
export function safeHref(raw: string): string | null {
  if (!raw) return null
  const stripped = decodeEntities(raw).replace(URL_NOISE_RE, "")
  if (!stripped) return null
  // Invalid in a URL anyway, and the characters that would matter if this
  // string ever did reach an HTML parser. Cheap, and it ends the argument.
  if (/[<>"`]/.test(stripped)) return null
  const scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(stripped)
  if (!scheme) return null
  if (!SAFE_SCHEMES.has(scheme[1].toLowerCase())) return null
  return stripped
}

/* ------------------------------------------------------------------ *
 * Inline
 * ------------------------------------------------------------------ */

const ASCII_PUNCT = /[!-/:-@[-`{-~]/

/** Above this many delimiter runs in one paragraph, emphasis matching is not
 *  worth its cost and the run characters render literally. Bounds the work a
 *  pathological message (ten thousand asterisks, arriving one token at a time)
 *  can ask for. */
const MAX_DELIMS = 2000

type DelimChar = "*" | "_" | "~"

type Tok = { k: "node"; node: InlineNode } | { k: "delim"; char: DelimChar; n: number; open: boolean; close: boolean }

function isWs(ch: string): boolean {
  return ch === "" || ch === " " || ch === "\t" || ch === "\n" || ch === "\r" || ch === "\f" || ch === "\u00a0"
}

function findClosingTicks(src: string, from: number, run: number): number {
  let i = from
  while (i < src.length) {
    if (src[i] !== "`") {
      i++
      continue
    }
    let n = 0
    while (src[i + n] === "`") n++
    if (n === run) return i
    i += n
  }
  return -1
}

/** Index of the `]` closing the `[` at `open`, or -1. Skips code spans,
 *  escapes and nested brackets, so ``[a `]` b](x)`` and `[a [b] c](x)` work. */
function findLabelEnd(src: string, open: number): number {
  let depth = 0
  let i = open
  while (i < src.length) {
    const c = src[i]
    if (c === "\\") {
      i += 2
      continue
    }
    if (c === "`") {
      let n = 0
      while (src[i + n] === "`") n++
      const close = findClosingTicks(src, i + n, n)
      i = close < 0 ? i + n : close + n
      continue
    }
    if (c === "[") depth++
    else if (c === "]") {
      depth--
      if (depth === 0) return i
    }
    i++
  }
  return -1
}

interface Dest {
  href: string | null
  title?: string
  end: number
}

/** Parses `(dest "title")`, starting just past the `(`. */
function parseDest(src: string, from: number): Dest | null {
  let i = from
  while (i < src.length && isWs(src[i])) i++
  let raw = ""
  if (src[i] === "<") {
    const close = src.indexOf(">", i + 1)
    if (close < 0) return null
    raw = src.slice(i + 1, close)
    if (raw.includes("\n")) return null
    i = close + 1
  } else {
    let depth = 0
    const start = i
    while (i < src.length) {
      const c = src[i]
      if (c === "\\" && i + 1 < src.length) {
        i += 2
        continue
      }
      if (c === "(") depth++
      else if (c === ")") {
        if (depth === 0) break
        depth--
      } else if (isWs(c)) break
      i++
    }
    raw = src.slice(start, i)
  }
  while (i < src.length && isWs(src[i])) i++
  let title: string | undefined
  const quote = src[i]
  if (quote === '"' || quote === "'") {
    const close = src.indexOf(quote, i + 1)
    if (close < 0) return null
    title = src.slice(i + 1, close)
    i = close + 1
    while (i < src.length && isWs(src[i])) i++
  }
  if (src[i] !== ")") return null
  return { href: safeHref(raw.replace(/\\(.)/g, "$1")), title, end: i + 1 }
}

/** Trailing punctuation that almost never belongs to a bare URL. Parentheses
 *  are balanced rather than trimmed, so a Wikipedia link survives. */
function trimBareUrl(url: string): string {
  let out = url
  for (;;) {
    const last = out[out.length - 1]
    if (last === undefined) break
    if (last === ")") {
      const opens = (out.match(/\(/g) ?? []).length
      const closes = (out.match(/\)/g) ?? []).length
      if (closes > opens) {
        out = out.slice(0, -1)
        continue
      }
      break
    }
    if (".,;:!?'\"]}*_~".includes(last)) {
      out = out.slice(0, -1)
      continue
    }
    break
  }
  return out
}

const BARE_URL_RE = /^(?:https?:\/\/|www\.)[^\s<]+/i
const ANGLE_AUTOLINK_RE = /^<([a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^\s<>]*)>/
const ANGLE_EMAIL_RE = /^<([^\s<>@]+@[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,})>/

function tokenize(src: string): Tok[] {
  const toks: Tok[] = []
  let text = ""
  let delims = 0

  const flush = (): void => {
    if (text) {
      toks.push({ k: "node", node: { type: "text", value: text } })
      text = ""
    }
  }
  const push = (node: InlineNode): void => {
    flush()
    toks.push({ k: "node", node })
  }

  let i = 0
  while (i < src.length) {
    const c = src[i]

    if (c === "\\") {
      const next = src[i + 1]
      if (next === "\n") {
        push({ type: "break" })
        i += 2
        continue
      }
      if (next !== undefined && ASCII_PUNCT.test(next)) {
        text += next
        i += 2
        continue
      }
      text += c
      i++
      continue
    }

    if (c === "\n") {
      // Two trailing spaces is a hard break and a lone newline is a soft one;
      // both render as a line break. This is a chat transcript — a model that
      // wrote two lines meant two lines.
      text = text.replace(/[ \t]+$/, "")
      push({ type: "break" })
      i++
      continue
    }

    if (c === "`") {
      let n = 0
      while (src[i + n] === "`") n++
      const close = findClosingTicks(src, i + n, n)
      if (close < 0) {
        text += "`".repeat(n)
        i += n
        continue
      }
      let value = src.slice(i + n, close).replace(/\n/g, " ")
      if (value.length >= 2 && value.startsWith(" ") && value.endsWith(" ") && value.trim() !== "") {
        value = value.slice(1, -1)
      }
      push({ type: "code", value })
      i = close + n
      continue
    }

    if (c === "<") {
      const rest = src.slice(i)
      const auto = ANGLE_AUTOLINK_RE.exec(rest)
      if (auto) {
        push({ type: "link", href: safeHref(auto[1]), children: [{ type: "text", value: auto[1] }] })
        i += auto[0].length
        continue
      }
      const mail = ANGLE_EMAIL_RE.exec(rest)
      if (mail) {
        push({ type: "link", href: safeHref(`mailto:${mail[1]}`), children: [{ type: "text", value: mail[1] }] })
        i += mail[0].length
        continue
      }
      // Not an autolink, so `<` is text. There is no HTML branch here to fall
      // into, which is the entire point of this module.
      text += c
      i++
      continue
    }

    if (c === "[" || (c === "!" && src[i + 1] === "[")) {
      const image = c === "!"
      const bracket = image ? i + 1 : i
      const end = findLabelEnd(src, bracket)
      if (end > 0 && src[end + 1] === "(") {
        const dest = parseDest(src, end + 2)
        if (dest) {
          let children = parseInline(src.slice(bracket + 1, end))
          if (children.length === 0) children = [{ type: "text", value: image ? "image" : (dest.href ?? "link") }]
          push({ type: "link", href: dest.href, title: dest.title, image, children })
          i = dest.end
          continue
        }
      }
      text += c
      i++
      continue
    }

    if ((c === "h" || c === "H" || c === "w" || c === "W") && (i === 0 || !/[\w@./:-]/.test(src[i - 1]))) {
      const m = BARE_URL_RE.exec(src.slice(i))
      if (m) {
        const url = trimBareUrl(m[0])
        const href = safeHref(url.toLowerCase().startsWith("www.") ? `https://${url}` : url)
        if (href) {
          push({ type: "link", href, children: [{ type: "text", value: url }] })
          i += url.length
          continue
        }
      }
    }

    if (c === "*" || c === "_" || c === "~") {
      let n = 0
      while (src[i + n] === c) n++
      // Three or more tildes is a fence that leaked into a paragraph, not
      // strikethrough.
      if (c === "~" && n > 2) {
        text += c.repeat(n)
        i += n
        continue
      }
      const before = i > 0 ? src[i - 1] : ""
      const after = i + n < src.length ? src[i + n] : ""
      const wsBefore = isWs(before)
      const wsAfter = isWs(after)
      const pBefore = before !== "" && ASCII_PUNCT.test(before)
      const pAfter = after !== "" && ASCII_PUNCT.test(after)
      const left = !wsAfter && (!pAfter || wsBefore || pBefore)
      const right = !wsBefore && (!pBefore || wsAfter || pAfter)
      const open = c === "_" ? left && (!right || pBefore) : left
      const close = c === "_" ? right && (!left || pAfter) : right
      if ((open || close) && delims < MAX_DELIMS) {
        flush()
        toks.push({ k: "delim", char: c as DelimChar, n, open, close })
        delims++
      } else {
        text += c.repeat(n)
      }
      i += n
      continue
    }

    text += c
    i++
  }

  flush()
  return toks
}

function mergeText(nodes: InlineNode[]): InlineNode[] {
  const out: InlineNode[] = []
  for (const node of nodes) {
    const prev = out[out.length - 1]
    if (node.type === "text" && prev && prev.type === "text") prev.value += node.value
    else out.push(node)
  }
  return out.filter((n) => n.type !== "text" || n.value !== "")
}

function collect(items: (Tok | null)[], from: number, to: number): InlineNode[] {
  const out: InlineNode[] = []
  for (let i = from; i < to; i++) {
    const t = items[i]
    if (!t) continue
    if (t.k === "node") out.push(t.node)
    else out.push({ type: "text", value: t.char.repeat(t.n) })
  }
  return mergeText(out)
}

/**
 * The delimiter-matching pass.
 *
 * Walks closers left to right and pairs each with the nearest preceding opener
 * of the same character — the standard shape of the CommonMark algorithm,
 * minus its "rule of three" refinement, which only changes the answer for
 * inputs like `*foo**bar**baz*` that this app's traffic does not contain.
 * Progress is guaranteed: every match consumes at least one delimiter from
 * each side, so the loop terminates on any input.
 */
function resolveEmphasis(toks: Tok[]): InlineNode[] {
  const items: (Tok | null)[] = toks.slice()
  let positions: number[] = []
  for (let i = 0; i < items.length; i++) if (items[i]?.k === "delim") positions.push(i)

  let ci = 0
  while (ci < positions.length) {
    const closerIdx = positions[ci]
    const closer = items[closerIdx]
    if (!closer || closer.k !== "delim" || !closer.close) {
      ci++
      continue
    }
    let found = -1
    for (let oi = ci - 1; oi >= 0; oi--) {
      const openerIdx = positions[oi]
      const opener = items[openerIdx]
      if (!opener || opener.k !== "delim") continue
      if (opener.char !== closer.char || !opener.open) continue
      // Adjacent runs enclose nothing; `****` is four literal asterisks.
      if (openerIdx + 1 >= closerIdx) continue
      found = openerIdx
      break
    }
    if (found < 0) {
      ci++
      continue
    }
    const opener = items[found] as Extract<Tok, { k: "delim" }>
    const use = closer.char === "~" ? Math.min(opener.n, closer.n, 2) : opener.n >= 2 && closer.n >= 2 ? 2 : 1
    const children = collect(items, found + 1, closerIdx)
    const wrapped: InlineNode =
      closer.char === "~"
        ? { type: "del", children }
        : use === 2
          ? { type: "strong", children }
          : { type: "em", children }

    for (let k = found + 1; k < closerIdx; k++) items[k] = null
    items[found + 1] = { k: "node", node: wrapped }
    opener.n -= use
    closer.n -= use
    if (opener.n === 0) items[found] = null
    if (closer.n === 0) items[closerIdx] = null

    positions = []
    for (let i = 0; i < items.length; i++) if (items[i]?.k === "delim") positions.push(i)
    ci = 0
    for (const p of positions) {
      if (p >= closerIdx) break
      ci++
    }
  }

  return collect(items, 0, items.length)
}

/** Parse a run of inline Markdown. Exported for one-line fields and for table
 *  cells, both of which have no block structure to speak of. */
export function parseInline(src: string): InlineNode[] {
  if (!src) return []
  return resolveEmphasis(tokenize(src))
}

/* ------------------------------------------------------------------ *
 * Blocks
 * ------------------------------------------------------------------ */

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})[ \t]*([^`]*)$/
const HEADING_RE = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/
const HR_RE = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/
const QUOTE_RE = /^ {0,3}>/
const LIST_RE = /^( {0,3})(?:([-*+])|(\d{1,9})([.)]))(?:([ \t]+)(.*)|[ \t]*())$/
const TABLE_DELIM_RE = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/

function isBlockStart(line: string): boolean {
  return FENCE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line) || QUOTE_RE.test(line) || LIST_RE.test(line)
}

function indentOf(line: string): number {
  let n = 0
  while (line[n] === " ") n++
  return n
}

function splitRow(line: string): string[] {
  let s = line.trim()
  if (s.startsWith("|")) s = s.slice(1)
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1)
  const cells: string[] = []
  let cur = ""
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "\\" && s[i + 1] === "|") {
      cur += "|"
      i++
      continue
    }
    if (s[i] === "|") {
      cells.push(cur)
      cur = ""
      continue
    }
    cur += s[i]
  }
  cells.push(cur)
  return cells.map((c) => c.trim())
}

function tryTable(lines: string[], at: number): { node: BlockNode; next: number } | null {
  const header = lines[at]
  const delim = lines[at + 1]
  if (!header || !delim || !header.includes("|")) return null
  if (!TABLE_DELIM_RE.test(delim)) return null
  const head = splitRow(header)
  const spec = splitRow(delim)
  if (head.length !== spec.length || head.length < 1) return null
  const align: TableAlign[] = spec.map((cell) => {
    const l = cell.startsWith(":")
    const r = cell.endsWith(":")
    return l && r ? "center" : r ? "right" : l ? "left" : null
  })
  const rows: InlineNode[][][] = []
  let j = at + 2
  for (; j < lines.length; j++) {
    const line = lines[j]
    if (!line.trim() || !line.includes("|")) break
    if (HR_RE.test(line) || FENCE_RE.test(line) || HEADING_RE.test(line)) break
    const cells = splitRow(line)
    while (cells.length < head.length) cells.push("")
    rows.push(cells.slice(0, head.length).map(parseInline))
  }
  return { node: { type: "table", align, head: head.map(parseInline), rows }, next: j }
}

function parseList(lines: string[], at: number): { node: BlockNode; next: number } {
  const first = LIST_RE.exec(lines[at]) as RegExpExecArray
  const ordered = Boolean(first[3])
  const marker = ordered ? first[4] : first[2]
  const start = ordered ? Number.parseInt(first[3], 10) : 1
  const items: BlockNode[][] = []
  let tight = true
  let i = at

  while (i < lines.length) {
    const m = LIST_RE.exec(lines[i])
    if (!m) break
    if (Boolean(m[3]) !== ordered) break
    if ((ordered ? m[4] : m[2]) !== marker) break

    const markerWidth = (m[1] ?? "").length + (ordered ? m[3].length + 1 : 1)
    const gap = (m[5] ?? " ").length
    // A marker followed by five or more spaces opens an indented block, not a
    // five-column content indent. Clamp it the way CommonMark does.
    const contentIndent = markerWidth + (gap > 4 ? 1 : gap)
    const body: string[] = [m[6] ?? ""]
    i++

    let blanks = 0
    while (i < lines.length) {
      const line = lines[i]
      if (!line.trim()) {
        blanks++
        i++
        continue
      }
      if (indentOf(line) >= contentIndent) {
        if (blanks) {
          tight = false
          for (let b = 0; b < blanks; b++) body.push("")
          blanks = 0
        }
        body.push(line.slice(contentIndent))
        i++
        continue
      }
      // A line back at column zero ends the item.
      //
      // This is the one place the parser knowingly disagrees with CommonMark,
      // which would treat it as lazy continuation text and fold it into the
      // item's paragraph. The message this whole feature exists for is exactly
      // that shape — `_compose_desktop_reply` in the API writes a numbered list
      // of desktop steps and then appends the handoff note on the next line,
      // with no blank line between them:
      //
      //     1. `open_chromium(...)` — ran
      //     2. `windows()` — ran
      //     **I need you at the screen.** …
      //
      // By the spec that last line belongs to item 2 and renders indented under
      // it. It is not a continuation of "windows() — ran"; it is the loudest
      // sentence in the product. Models compose by line, not by the spec's
      // paragraph rules, and an unindented line after a list is far more often
      // a new thought than a wrapped one. An indented continuation still works,
      // which is the case a model that *does* mean to continue produces.
      break
    }
    if (blanks > 0 && i < lines.length && LIST_RE.test(lines[i])) tight = false

    items.push(parseBlocks(body))
  }

  return { node: { type: "list", ordered, start, tight, items }, next: i }
}

function parseBlocks(lines: string[]): BlockNode[] {
  const out: BlockNode[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      i++
      continue
    }

    const fence = FENCE_RE.exec(line)
    if (fence) {
      const marker = fence[1][0]
      const len = fence[1].length
      const info = (fence[2] ?? "").trim()
      const lang = info ? (info.split(/\s+/)[0] ?? null) : null
      const closeRe = new RegExp(`^ {0,3}${marker}{${len},}[ \\t]*$`)
      const body: string[] = []
      let j = i + 1
      let closed = false
      for (; j < lines.length; j++) {
        if (closeRe.test(lines[j])) {
          closed = true
          break
        }
        body.push(lines[j])
      }
      // `open: true` is the streaming case: the fence has arrived and its
      // closing partner has not. Rendering it as a code block that is still
      // filling up is stable; re-reading it as paragraphs every token is not.
      out.push({ type: "code", lang, value: body.join("\n"), open: !closed })
      i = closed ? j + 1 : j
      continue
    }

    const heading = HEADING_RE.exec(line)
    if (heading) {
      const text = (heading[2] ?? "").replace(/[ \t]+#+[ \t]*$/, "")
      out.push({ type: "heading", level: heading[1].length, children: parseInline(text) })
      i++
      continue
    }

    if (HR_RE.test(line)) {
      out.push({ type: "hr" })
      i++
      continue
    }

    if (QUOTE_RE.test(line)) {
      const inner: string[] = []
      let j = i
      // No lazy continuation here either, for the same reason as in
      // `parseList`: a line that dropped the `>` is a new thought.
      for (; j < lines.length && QUOTE_RE.test(lines[j]); j++) {
        inner.push(lines[j].replace(/^ {0,3}> ?/, ""))
      }
      out.push({ type: "quote", children: parseBlocks(inner) })
      i = j
      continue
    }

    const table = tryTable(lines, i)
    if (table) {
      out.push(table.node)
      i = table.next
      continue
    }

    if (LIST_RE.test(line)) {
      const list = parseList(lines, i)
      out.push(list.node)
      i = list.next
      continue
    }

    const buf: string[] = []
    let j = i
    for (; j < lines.length; j++) {
      const l = lines[j]
      if (!l.trim()) break
      if (j > i && (isBlockStart(l) || tryTable(lines, j))) break
      buf.push(l)
    }
    out.push({ type: "paragraph", children: parseInline(buf.join("\n")) })
    i = j
  }

  return out
}

/**
 * Parse a Markdown document into blocks.
 *
 * Safe on partial input, and cheap enough to run on every streamed token: one
 * linear pass over the lines and one linear pass per paragraph.
 */
export function parseMarkdown(src: string): BlockNode[] {
  if (!src) return []
  const lines = src.replace(/\r\n?/g, "\n").replace(/\t/g, "    ").split("\n")
  return parseBlocks(lines)
}

/** Flatten to the text a screen reader — or an assertion — should see. */
export function inlineText(nodes: InlineNode[]): string {
  let out = ""
  for (const node of nodes) {
    switch (node.type) {
      case "text":
      case "code":
        out += node.value
        break
      case "break":
        out += " "
        break
      default:
        out += inlineText(node.children)
    }
  }
  return out
}
