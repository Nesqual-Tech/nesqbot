#!/usr/bin/env node
/**
 * Assertions for the Markdown renderer.
 *
 *   node apps/desktop/scripts/check-markdown.mjs
 *   node apps/desktop/scripts/check-markdown.mjs --verbose
 *
 * Three groups, in order of how badly they would bite:
 *
 *   1. **Injection.** This is the reason the parser exists in the shape it
 *      does. Everything rendered through it is model output, some of it read
 *      off a web page by a bot, and it lands in a Tauri webview that holds a
 *      production session and can invoke Rust commands. Raw HTML must stay
 *      text; `javascript:` and `data:` must never become an `href`, including
 *      when they are entity-encoded, whitespace-broken or wearing mixed case.
 *   2. **Rendering.** The features the chat transcript actually uses, plus the
 *      exact message from the bug report this work came from.
 *   3. **Streaming.** The parser is called on partial input once per token.
 *      Half a fenced code block must be a code block, not an exception, and
 *      the parse has to be cheap enough that doing it every token is fine.
 *
 * Runs on Node's built-in TypeScript stripping (Node 22.18+ / 24) — same
 * approach as `packages/ui/scripts/check-brand.mjs`, so there is no build step
 * and no test dependency. The resolver hook exists only because the app's
 * imports are extensionless for the bundler.
 */

import { registerHooks } from "node:module"
import { existsSync } from "node:fs"
import { fileURLToPath, pathToFileURL } from "node:url"
import { dirname, resolve } from "node:path"

registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith(".") && !/\.[cm]?[jt]sx?$/.test(specifier) && context.parentURL) {
      const base = new URL(specifier, context.parentURL)
      for (const ext of [".ts", ".tsx", "/index.ts"]) {
        const candidate = new URL(base.href + ext)
        if (existsSync(fileURLToPath(candidate))) return next(candidate.href, context)
      }
    }
    return next(specifier, context)
  },
})

const verbose = process.argv.includes("--verbose")
const here = dirname(fileURLToPath(import.meta.url))
const md = await import(pathToFileURL(resolve(here, "../src/lib/markdown.ts")).href)

const { parseMarkdown, parseInline, safeHref, decodeEntities, inlineText } = md

let failures = 0
let checks = 0

function ok(label, condition, detail) {
  checks++
  if (condition) {
    if (verbose) console.log(`  ok   ${label}`)
    return
  }
  failures++
  console.log(`  FAIL ${label}${detail === undefined ? "" : ` — ${detail}`}`)
}

function group(name) {
  console.log(`\n${name}`)
}

/** Every node in a parsed tree, flattened. */
function walk(nodes, out = []) {
  for (const node of nodes ?? []) {
    out.push(node)
    if (node.children) walk(node.children, out)
    if (node.items) for (const item of node.items) walk(item, out)
    if (node.head) for (const cell of node.head) walk(cell, out)
    if (node.rows) for (const row of node.rows) for (const cell of row) walk(cell, out)
  }
  return out
}

/** All link nodes anywhere in a document. */
function links(src) {
  return walk(parseMarkdown(src)).filter((n) => n.type === "link")
}

/** The text a reader would end up seeing, blocks flattened. */
function visibleText(src) {
  return walk(parseMarkdown(src))
    .filter((n) => n.type === "text" || n.type === "code")
    .map((n) => n.value)
    .join("")
}

/** Types present in the tree, as a set of strings. */
function types(src) {
  return new Set(walk(parseMarkdown(src)).map((n) => n.type))
}

/* ================================================================== *
 * 1. Injection
 * ================================================================== */

group("Injection — raw HTML")

{
  // The parser has no HTML branch at all, so this is really an assertion that
  // nothing grew one. A `<script>` may only ever be a text node, and the React
  // renderer escapes text nodes.
  const src = "<script>alert(document.cookie)</script>"
  const nodes = walk(parseMarkdown(src))
  ok(
    "<script> produces no element node",
    nodes.every((n) => ["paragraph", "text", "break"].includes(n.type)),
  )
  ok("<script> survives only as literal text", visibleText(src).includes("<script>alert(document.cookie)</script>"))
}

{
  const src = '<img src=x onerror="alert(1)">'
  const nodes = walk(parseMarkdown(src))
  ok(
    "onerror image produces no element node",
    nodes.every((n) => ["paragraph", "text", "break"].includes(n.type)),
  )
  ok("onerror image is literal text", visibleText(src).includes("onerror="))
  ok("onerror image yields no link", links(src).length === 0)
}

{
  const src = "&lt;script&gt;alert(1)&lt;/script&gt;"
  // Entity-encoded HTML must not be decoded back into markup — and it cannot
  // be, because there is no markup path. It stays a string either way.
  const nodes = walk(parseMarkdown(src))
  ok(
    "HTML-encoded script produces no element node",
    nodes.every((n) => ["paragraph", "text", "break"].includes(n.type)),
  )
}

{
  const src = '<iframe src="https://evil.example"></iframe><svg onload=alert(1)>'
  const nodes = walk(parseMarkdown(src))
  ok(
    "iframe/svg produce no element nodes",
    nodes.every((n) => ["paragraph", "text", "break"].includes(n.type)),
  )
}

{
  // A model can also emit HTML *inside* a fence. It must stay inside it.
  const src = "```html\n<script>alert(1)</script>\n```"
  const blocks = parseMarkdown(src)
  ok("HTML in a fence stays a code block", blocks.length === 1 && blocks[0].type === "code")
  ok("HTML in a fence keeps its text", blocks[0].value === "<script>alert(1)</script>")
}

group("Injection — link targets")

const BLOCKED = [
  ["javascript:", "[x](javascript:alert(1))"],
  ["JaVaScRiPt: mixed case", "[x](JaVaScRiPt:alert(1))"],
  ["leading whitespace", "[x](   javascript:alert(1))"],
  ["tab inside the scheme", "[x](java\tscript:alert(1))"],
  ["newline inside the scheme", "[x](<java\nscript:alert(1)>)"],
  ["NUL inside the scheme", "[x](java\u0000script:alert(1))"],
  ["entity-encoded tab", "[x](java&#x09;script:alert(1))"],
  ["entity-encoded colon", "[x](javascript&colon;alert(1))"],
  ["decimal entity first letter", "[x](&#106;avascript:alert(1))"],
  ["hex entity first letter", "[x](&#x6a;avascript:alert(1))"],
  ["double-encoded", "[x](&amp;#x6a;avascript:alert(1))"],
  ["zero-width space", "[x](java\u200bscript:alert(1))"],
  ["data: html", "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)"],
  ["data: svg", "[x](data:image/svg+xml,<svg onload=alert(1)>)"],
  ["vbscript:", "[x](vbscript:msgbox(1))"],
  ["file:", "[x](file:///C:/Windows/System32/calc.exe)"],
  ["blob:", "[x](blob:https://evil.example/abc)"],
  ["nesqbot: deep link", "[x](nesqbot://auth?code=stolen)"],
  ["protocol-relative", "[x](//evil.example/steal)"],
  ["in-app navigation", "[x](/settings)"],
  ["angle autolink javascript:", "<javascript:alert(1)>"],
  ["image with javascript:", "![x](javascript:alert(1))"],
  ["reference-style is not resolved", "[x][ref]\n\n[ref]: javascript:alert(1)"],
]

for (const [label, src] of BLOCKED) {
  const found = links(src)
  const live = found.filter((l) => l.href !== null)
  ok(`refused: ${label}`, live.length === 0, live.map((l) => l.href).join(", "))
}

{
  // Refusing must not mean deleting. The words stay; only the target goes.
  const nodes = links("[click here](javascript:alert(1))")
  ok("a refused link keeps its text", nodes.length === 1 && inlineText(nodes[0].children) === "click here")
  ok("a refused link has href null", nodes[0].href === null)
}

const ALLOWED = [
  ["https", "[x](https://example.com/a?b=c#d)", "https://example.com/a?b=c#d"],
  ["http", "[x](http://example.com/)", "http://example.com/"],
  ["mailto", "[x](mailto:a@b.com)", "mailto:a@b.com"],
  ["angle autolink", "<https://example.com/x>", "https://example.com/x"],
  ["bare url", "see https://example.com/x now", "https://example.com/x"],
  ["angle email", "<a@b.com>", "mailto:a@b.com"],
]

for (const [label, src, expected] of ALLOWED) {
  const found = links(src)
  ok(`allowed: ${label}`, found.length === 1 && found[0].href === expected, found.map((l) => l.href).join(", "))
}

{
  ok("safeHref is exported and refuses javascript:", safeHref("javascript:alert(1)") === null)
  ok("safeHref refuses an empty string", safeHref("") === null)
  ok("safeHref keeps a normal URL intact", safeHref("https://example.com/a b") === "https://example.com/ab")
  ok("decodeEntities resolves one level only", decodeEntities("&amp;#x6a;") === "&#x6a;")
}

{
  // Titles are attacker-controlled too. They only ever reach a `title`
  // attribute, but assert they are not being used as a second destination.
  const [link] = links('[x](https://example.com "a\\" onmouseover=alert(1)")')
  ok("a link title cannot smuggle a second attribute", link !== undefined && link.href === "https://example.com")
}

/* ================================================================== *
 * 2. Rendering
 * ================================================================== */

group("Rendering — the message from the bug report")

const BUG = [
  "**On my desktop this turn:**",
  "1. `open_chromium(text='https://www.linkedin.com/login')` — ran",
  "2. `windows()` — ran",
  "**I need you at the screen.** LinkedIn needs your login credentials…",
].join("\n")

{
  const blocks = parseMarkdown(BUG)
  const kinds = blocks.map((b) => b.type)
  ok(
    "the report parses to paragraph + list + paragraph",
    kinds.join(",") === "paragraph,list,paragraph",
    kinds.join(","),
  )

  const [lead, list, tail] = blocks
  ok("the lead is a single strong run", lead.children.length === 1 && lead.children[0].type === "strong")
  ok("the lead says the right thing", inlineText(lead.children) === "On my desktop this turn:")

  ok("the list is ordered and starts at 1", list.ordered === true && list.start === 1)
  ok("the list is tight", list.tight === true)
  ok("the list has two items", list.items.length === 2)

  const first = list.items[0][0]
  ok("item 1 is a paragraph", first.type === "paragraph")
  ok("item 1 opens with a code span", first.children[0].type === "code")
  ok(
    "item 1's code span is the call",
    first.children[0].value === "open_chromium(text='https://www.linkedin.com/login')",
    first.children[0].value,
  )
  ok("item 1's tail is plain", inlineText(first.children.slice(1)) === " — ran")

  const second = list.items[1][0]
  ok("item 2's code span is windows()", second.children[0].type === "code" && second.children[0].value === "windows()")

  ok("the tail opens with a strong run", tail.children[0].type === "strong")
  ok("the tail's bold text is right", inlineText([tail.children[0]]) === "I need you at the screen.")
  ok(
    "the tail's remaining prose is intact",
    inlineText(tail.children.slice(1)) === " LinkedIn needs your login credentials…",
    JSON.stringify(inlineText(tail.children.slice(1))),
  )

  // The whole point: not one asterisk or backtick left visible.
  const visible = visibleText(BUG)
  ok("no asterisks survive into the visible text", !visible.includes("*"))
  ok("no backticks survive into the visible text", !visible.includes("`"))
}

group("Rendering — coverage")

ok("bold", types("**b**").has("strong"))
ok("italic with asterisks", types("*i*").has("em"))
ok("italic with underscores", types("_i_").has("em"))
ok("bold inside italic", inlineText(parseInline("*a **b** c*")) === "a b c" && types("*a **b** c*").has("strong"))
ok("strikethrough", types("~~gone~~").has("del"))
ok("inline code", types("`x`").has("code"))
ok("inline code keeps its asterisks literal", parseInline("`**not bold**`")[0].value === "**not bold**")
ok("escaped asterisks stay literal", inlineText(parseInline("\\*not italic\\*")) === "*not italic*")

{
  const blocks = parseMarkdown("```python\nprint('hi')\n```")
  ok("fenced code block", blocks[0].type === "code" && blocks[0].value === "print('hi')")
  ok("fenced code language", blocks[0].lang === "python")
  ok("fenced code is closed", blocks[0].open === false)
}

{
  const blocks = parseMarkdown("- one\n- two\n  - nested\n- three")
  ok("unordered list", blocks[0].type === "list" && blocks[0].ordered === false)
  ok("unordered list length", blocks[0].items.length === 3)
  const nested = blocks[0].items[1].find((b) => b.type === "list")
  ok("nested list", nested !== undefined && nested.items.length === 1)
}

{
  const blocks = parseMarkdown("3. three\n4. four")
  ok("ordered list honours its start", blocks[0].type === "list" && blocks[0].start === 3)
}

{
  const blocks = parseMarkdown("- one\n\n- two")
  ok("a blank line between items makes the list loose", blocks[0].tight === false)
}

{
  const blocks = parseMarkdown("# h1\n## h2\n###### h6")
  ok("headings", blocks.map((b) => b.type).join(",") === "heading,heading,heading")
  ok("heading levels", blocks.map((b) => b.level).join(",") === "1,2,6")
  ok("a hash with no space is not a heading", parseMarkdown("#nope")[0].type === "paragraph")
}

{
  const blocks = parseMarkdown("a\n\n---\n\nb")
  ok("thematic break", blocks.map((b) => b.type).join(",") === "paragraph,hr,paragraph")
}

{
  const blocks = parseMarkdown("first\n\nsecond")
  ok("paragraphs split on a blank line", blocks.length === 2)
  const one = parseMarkdown("first\nsecond")
  ok("a single newline stays one paragraph", one.length === 1)
  ok(
    "a single newline becomes a line break",
    one[0].children.some((n) => n.type === "break"),
  )
}

{
  const blocks = parseMarkdown("> quoted\n> more")
  ok("blockquote", blocks[0].type === "quote" && blocks[0].children[0].type === "paragraph")
}

{
  const blocks = parseMarkdown("| a | b |\n| --- | ---: |\n| 1 | 2 |")
  ok("table", blocks[0].type === "table")
  ok("table alignment", blocks[0].align.join(",") === ",right")
  ok("table rows", blocks[0].rows.length === 1 && inlineText(blocks[0].rows[0][1]) === "2")
}

{
  // The API's own composed reply: prose, a rule, then the transcript.
  const src = "Signed in.\n\n---\n**On my desktop this turn:**\n1. `click(x=10, y=20)` — ran"
  const kinds = parseMarkdown(src).map((b) => b.type)
  ok("prose + rule + report", kinds.join(",") === "paragraph,hr,paragraph,list", kinds.join(","))
}

ok("empty input parses to nothing", parseMarkdown("").length === 0)
ok("whitespace-only input parses to nothing", parseMarkdown("   \n\n  ").length === 0)

/* ================================================================== *
 * 3. Streaming
 * ================================================================== */

group("Streaming")

{
  const partial = "Here you go:\n\n```python\nprint('hi')"
  const blocks = parseMarkdown(partial)
  ok("an unterminated fence is still a code block", blocks[1]?.type === "code")
  ok("an unterminated fence is marked open", blocks[1]?.open === true)
  ok("an unterminated fence keeps what has arrived", blocks[1]?.value === "print('hi')")
}

{
  // Every prefix of the bug-report message must parse without throwing, and
  // the block count must not thrash: this is the "does not flicker the whole
  // transcript" property, checked on the shape rather than on pixels.
  let threw = null
  const counts = []
  for (let i = 0; i <= BUG.length; i++) {
    try {
      counts.push(parseMarkdown(BUG.slice(0, i)).length)
    } catch (err) {
      threw = `at ${i}: ${err}`
      break
    }
  }
  ok("every prefix of the report parses", threw === null, threw ?? "")

  /*
   * The block count is allowed to dip exactly once, and only for the reason
   * below. Between `2` and `2.` arriving, the bare digit is prose, so the list
   * has closed and a new paragraph has opened — three blocks. The `.` turns it
   * back into a list marker and the paragraph is reabsorbed — two blocks. One
   * character wide, unavoidable without buffering the whole stream, and
   * invisible at real token granularity.
   *
   * Anything beyond that is the failure this checks for: a shape that
   * oscillates while tokens arrive, which is what makes a transcript flicker.
   */
  const dips = counts.map((n, i) => (i > 0 && n < counts[i - 1] ? i : -1)).filter((i) => i >= 0)
  ok("the block shape settles as it streams", dips.length <= 1, `dips at ${dips.join(", ")}`)
  ok("the final shape is the parsed shape", counts[counts.length - 1] === parseMarkdown(BUG).length)
}

{
  // The same, for a message that is mostly a code block — the case where a
  // naive parser oscillates between <pre> and paragraphs on every token.
  const src = "Run this:\n\n```bash\nnpm run tauri build\ncd apps/desktop\n```\n\nThen restart."
  const shapes = new Set()
  for (let i = 0; i <= src.length; i++) {
    const blocks = parseMarkdown(src.slice(0, i))
    const code = blocks.filter((b) => b.type === "code").length
    shapes.add(code)
  }
  ok(
    "a streaming fence is never more than one code block",
    [...shapes].every((n) => n <= 1),
    [...shapes].join(","),
  )
}

{
  const src = BUG.repeat(40) // ~8 KB, well past a realistic turn
  const iterations = 200
  const t0 = process.hrtime.bigint()
  for (let i = 0; i < iterations; i++) parseMarkdown(src)
  const perParse = Number(process.hrtime.bigint() - t0) / iterations / 1e6
  console.log(`  parse: ${perParse.toFixed(3)} ms for ${src.length} bytes`)
  // Generous by ~an order of magnitude, so this is a regression alarm and not
  // a benchmark of the machine it happens to run on.
  ok("parsing 8 KB stays under 5 ms", perParse < 5, `${perParse.toFixed(3)} ms`)
}

{
  // Pathological input must not be able to hang the render loop.
  const nasty = "*".repeat(20000)
  const t0 = process.hrtime.bigint()
  parseMarkdown(nasty)
  const ms = Number(process.hrtime.bigint() - t0) / 1e6
  console.log(`  parse: ${ms.toFixed(3)} ms for 20000 unmatched asterisks`)
  ok("20k unmatched delimiters stay under 100 ms", ms < 100, `${ms.toFixed(3)} ms`)

  const nested = "[".repeat(5000) + "x"
  const t1 = process.hrtime.bigint()
  parseMarkdown(nested)
  const ms1 = Number(process.hrtime.bigint() - t1) / 1e6
  ok("5k unclosed brackets stay under 100 ms", ms1 < 100, `${ms1.toFixed(3)} ms`)
}

/* ================================================================== */

console.log(`\n${checks - failures}/${checks} checks passed`)
if (failures > 0) {
  console.log(`${failures} FAILED`)
  process.exit(1)
}
