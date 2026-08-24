/**
 * The Markdown renderer.
 *
 * `lib/markdown.ts` turns text into a typed node tree; this file turns that
 * tree into React elements. Between the two there is no HTML string, so there
 * is no `dangerouslySetInnerHTML` here and no sanitiser to misconfigure — a
 * `<script>` in the source can only arrive as a `text` node, and React escapes
 * text nodes. The read that matters when reviewing this file is that nothing
 * below ever builds markup from a string.
 *
 * Two more rules live here rather than in the parser:
 *
 *  - **Links leave the app.** A bare `<a href>` in a Tauri webview navigates
 *    the webview, which would replace Nesq Bot with whatever page a bot read
 *    off the internet. Every click is intercepted and handed to `openExternal`.
 *  - **A refused URL is still readable.** `safeHref` returns `null` for
 *    `javascript:`, `data:` and everything else off the allowlist. The link
 *    text is still shown — struck through and titled — because silently
 *    deleting content a bot produced is its own kind of lie.
 */
import { Fragment, memo, useMemo, type ReactNode } from "react"
import { parseInline, parseMarkdown, type BlockNode, type InlineNode } from "../lib/markdown"
import { openExternal } from "../lib/tauri"
import { cx } from "../lib/format"

function Link({ href, title, children }: { href: string; title?: string; children: ReactNode }) {
  const leave = (event: { preventDefault: () => void; stopPropagation: () => void }): void => {
    // Both halves matter. `preventDefault` is what stops the webview
    // navigating; `stopPropagation` keeps the click off the surrounding card,
    // several of which are themselves clickable.
    event.preventDefault()
    event.stopPropagation()
    void openExternal(href).catch(() => undefined)
  }
  return (
    <a
      className="md__link"
      href={href}
      title={title ?? href}
      rel="noreferrer noopener"
      onClick={leave}
      // Middle-click raises `auxclick`, not `click`, and would otherwise reach
      // the webview's own handling for it.
      onAuxClick={leave}
    >
      {children}
    </a>
  )
}

function BlockedLink({ children }: { children: ReactNode }) {
  return (
    <span className="md__link md__link--blocked" title="Link removed — only http, https and mailto links can be opened">
      {children}
    </span>
  )
}

function renderInline(nodes: InlineNode[], keyPrefix = ""): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${keyPrefix}${index}`
    switch (node.type) {
      case "text":
        // A Fragment, not a span: text runs must not become elements, or
        // `overflow-wrap` and selection start behaving differently inside a
        // bubble than in the rest of the app.
        return <Fragment key={key}>{node.value}</Fragment>
      case "break":
        return <br key={key} />
      case "code":
        return (
          <code key={key} className="md__code">
            {node.value}
          </code>
        )
      case "strong":
        return <strong key={key}>{renderInline(node.children, `${key}.`)}</strong>
      case "em":
        return <em key={key}>{renderInline(node.children, `${key}.`)}</em>
      case "del":
        return <del key={key}>{renderInline(node.children, `${key}.`)}</del>
      case "link": {
        const inner = renderInline(node.children, `${key}.`)
        if (!node.href) return <BlockedLink key={key}>{inner}</BlockedLink>
        return (
          <Link key={key} href={node.href} title={node.title}>
            {node.image ? <span className="md__image-mark">image: </span> : null}
            {inner}
          </Link>
        )
      }
    }
  })
}

function renderBlocks(nodes: BlockNode[], keyPrefix = ""): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${keyPrefix}${index}`
    switch (node.type) {
      case "paragraph":
        return <p key={key}>{renderInline(node.children, `${key}.`)}</p>

      case "heading": {
        const level = Math.min(6, Math.max(1, node.level))
        const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6"
        return (
          <Tag key={key} className={`md__h md__h--${level}`}>
            {renderInline(node.children, `${key}.`)}
          </Tag>
        )
      }

      case "code":
        // The wrapper carries the frame and the language chip; the `<pre>`
        // inside it is the only thing that scrolls. Putting the chip on the
        // `<pre>` itself was wrong twice over: it overlapped the first line,
        // and it slid away with the content on a wide block.
        return (
          <div
            key={key}
            className={cx("md__codeblock", node.lang && "md__codeblock--labelled", node.open && "md__codeblock--open")}
          >
            {node.lang ? <span className="md__lang">{node.lang}</span> : null}
            <pre className="md__pre">
              <code>{node.value}</code>
            </pre>
          </div>
        )

      case "hr":
        return <hr key={key} className="md__hr" />

      case "quote":
        return (
          <blockquote key={key} className="md__quote">
            {renderBlocks(node.children, `${key}.`)}
          </blockquote>
        )

      case "list": {
        const items = node.items.map((blocks, i) => (
          <li key={`${key}.${i}`}>
            {node.tight ? renderTight(blocks, `${key}.${i}.`) : renderBlocks(blocks, `${key}.${i}.`)}
          </li>
        ))
        return node.ordered ? (
          <ol key={key} className="md__ol" start={node.start === 1 ? undefined : node.start}>
            {items}
          </ol>
        ) : (
          <ul key={key} className="md__ul">
            {items}
          </ul>
        )
      }

      case "table":
        return (
          <div key={key} className="md__table-scroll">
            <table className="md__table">
              <thead>
                <tr>
                  {node.head.map((cell, i) => (
                    <th key={`${key}.h${i}`} style={node.align[i] ? { textAlign: node.align[i] as "left" } : undefined}>
                      {renderInline(cell, `${key}.h${i}.`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {node.rows.map((row, r) => (
                  <tr key={`${key}.r${r}`}>
                    {row.map((cell, c) => (
                      <td
                        key={`${key}.r${r}c${c}`}
                        style={node.align[c] ? { textAlign: node.align[c] as "left" } : undefined}
                      >
                        {renderInline(cell, `${key}.r${r}c${c}.`)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
    }
  })
}

/** A tight list item drops the `<p>` around its first paragraph, which is what
 *  keeps `1. one` / `2. two` from being double-spaced. */
function renderTight(blocks: BlockNode[], keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = []
  blocks.forEach((block, index) => {
    if (block.type === "paragraph") out.push(...renderInline(block.children, `${keyPrefix}${index}.`))
    else out.push(...renderBlocks([block], `${keyPrefix}${index}-`))
  })
  return out
}

export interface MarkdownProps {
  text: string | null | undefined
  /**
   * Inline-only. For one-line fields — a takeover reason, a routine
   * description — where a `<p>`/`<ul>` would fight the surrounding layout.
   * Emphasis, code spans and links still work; headings and lists do not.
   */
  inline?: boolean
  className?: string
}

/**
 * Render Markdown.
 *
 * ## Streaming
 *
 * This runs on partial input, once per arriving token, so the choice was
 * "parse only when the value settles" or "make the parse cheap enough that it
 * does not matter". It is the second, for one reason: deferring the parse means
 * the bubble shows raw `**asterisks**` while the tokens land and then snaps
 * into formatted text at the end, which is a worse artefact than the one it
 * avoids. What makes it affordable:
 *
 *  - The parse is a single linear pass: 0.28 ms for 7 KB, which is a bigger
 *    message than this product produces, and 0.24 ms for twenty thousand
 *    unmatched asterisks. Both are measured by `scripts/check-markdown.mjs`,
 *    which fails if either regresses.
 *  - `useMemo` on `text` means an unrelated re-render of the transcript does
 *    not re-parse anything.
 *  - `MessageBubble` is `memo`ised, so a token arriving in the live bubble
 *    re-parses *that* bubble and touches no other. The transcript does not
 *    flicker because the transcript does not re-render.
 *  - An unterminated fence stays a code block (`open: true` in the parser)
 *    rather than oscillating between `<pre>` and a run of paragraphs on every
 *    token, which was the one case that genuinely would have flickered.
 */
export const Markdown = memo(function Markdown({ text, inline = false, className }: MarkdownProps) {
  const source = text ?? ""
  const nodes = useMemo(() => (inline ? parseInline(source) : parseMarkdown(source)), [source, inline])

  if (inline) {
    return <span className={cx("md", "md--inline", className)}>{renderInline(nodes as InlineNode[])}</span>
  }
  return <div className={cx("md", className)}>{renderBlocks(nodes as BlockNode[])}</div>
})
