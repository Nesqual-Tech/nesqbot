/**
 * One place to reach everything, without knowing where anything is.
 *
 * ## The problem
 *
 * This app has six tabs, a variable list of teammates, a desktop pane with its
 * own lifecycle, a theme, and a handful of things that only exist as a button
 * inside a panel you have to be on first. Every one of those is discoverable by
 * looking, which is fine the first time and slow every time after. Someone who
 * uses this daily should be able to say where they want to go and be there.
 *
 * ## The shape
 *
 * Deliberately not a fuzzy-matching, plugin-hosting, scored-ranking palette. It
 * is a filtered list with three rules:
 *
 *  1. **Subsequence matching**, not substring. "vs" finds "Vesna · Sales" and
 *     "ap" finds Approvals. It is the behaviour people have been trained to
 *     expect by every editor, and it is fifteen lines.
 *  2. **Prefix beats interior.** A query that starts a word ranks above one
 *     that appears in the middle, so typing "us" puts Usage above "Pause".
 *     Nothing else is scored: invented relevance is how palettes start
 *     surprising people.
 *  3. **Nothing here decides anything.** No command in this list approves,
 *     rejects, deletes or spends. A palette is a navigation surface and a
 *     keystroke away from a `delete` approval is not a shortcut, it is a
 *     hazard. Destructive actions stay where their consequences are written
 *     down.
 *
 * ## Why it renders its own overlay rather than a `<dialog>`
 *
 * `showModal()` gives focus trapping and an inert background for free, and it
 * would be the right answer in a browser. In this app the palette has to be
 * able to appear over the Bot Desktop pane while a takeover is live, and a
 * top-layer dialog blocks the pointer events that pane forwards to the bot's
 * machine even after it closes in some WebView builds. The manual version is
 * twenty lines and does not have that failure mode.
 */
import { useEffect, useMemo, useRef, useState } from "react"
import { cx } from "../lib/format"
import { dur, ease, gsap, useGSAP } from "../lib/motion"
import { Icon, type IconName } from "./Icon"

export interface Command {
  id: string
  label: string
  /** Second line: what this is, or which teammate it belongs to. */
  detail?: string
  /** Right-hand slot: the keyboard shortcut, when there is one. */
  shortcut?: string
  group: string
  glyph?: IconName
  /** Extra words to match against that are not shown. */
  keywords?: string
  run: () => void
}

export interface CommandPaletteProps {
  open: boolean
  onClose: () => void
  commands: Command[]
}

/** First letter of each word, so "vs" can find "Vesna · Sales". */
function initialsOf(haystack: string): string {
  return (haystack.toLowerCase().match(/(?:^|[\s·—:/-])([a-z0-9])/g) ?? []).map((s) => s.trim().slice(-1)).join("")
}

/**
 * Two match rules, and deliberately not a third.
 *
 * A **substring** match, ranked by where it lands and bonused when it begins a
 * word; and an **initials** match, so "vs" finds "Vesna · Sales" and "bd"
 * finds "Bot Desktop". Lower is better; `null` means no match.
 *
 * The obvious third rule — a free subsequence match, the classic fuzzy finder —
 * was tried and removed. Against a list this short it matches almost
 * everything: "des" found "Ada · Chief of Staff" and "Reload everything",
 * because those letters appear in that order somewhere. A palette that answers
 * a three-letter query with five unrelated rows is slower than the sidebar it
 * was meant to replace, and it teaches people not to trust the first result —
 * which is the only result that matters when the next keystroke is Enter.
 */
function rank(haystack: string, needle: string): number | null {
  if (!needle) return 0
  const h = haystack.toLowerCase()
  const n = needle.toLowerCase()

  const direct = h.indexOf(n)
  if (direct !== -1) {
    const startsWord = direct === 0 || /[\s·—:/-]/.test(h[direct - 1])
    return direct - (startsWord ? 1000 : 0)
  }

  const initials = initialsOf(haystack)
  const acronym = initials.indexOf(n)
  if (acronym !== -1 && n.length > 1) return 2000 + acronym

  return null
}

export function CommandPalette({ open, onClose, commands }: CommandPaletteProps) {
  const [query, setQuery] = useState("")
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLUListElement | null>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)
  /** Where focus was before the palette took it. Put back on close. */
  const restoreTo = useRef<HTMLElement | null>(null)

  const results = useMemo(() => {
    /*
     * The group name is NOT part of the haystack. It was, and it meant every
     * command in "Actions" matched a query containing an s, a c or a t.
     */
    const scored: Array<{ command: Command; score: number; index: number }> = []
    commands.forEach((command, index) => {
      const hay = `${command.label} ${command.detail ?? ""} ${command.keywords ?? ""}`
      const score = rank(hay, query.trim())
      if (score !== null) scored.push({ command, score, index })
    })

    /*
     * Grouped, then ranked — in that order, and it matters.
     *
     * Ranking globally and printing a header whenever the group changed gave
     * "Actions / Teammates / Actions / Teammates / Actions" down a five-row
     * list: the same two headers four times, and no way to skim. So the group
     * whose best match is best goes first, and its members follow together.
     * Within a group it is score, then authored order — which for an empty
     * query is the sidebar's order, which is the order people already know.
     */
    const best = new Map<string, number>()
    const groupOrder = new Map<string, number>()
    for (const entry of scored) {
      const current = best.get(entry.command.group)
      if (current === undefined || entry.score < current) best.set(entry.command.group, entry.score)
      // Ties break on where the group was authored, not on its name.
      // Alphabetical put "Actions" above "Teammates" on an empty query, which
      // greets everyone with "Reload everything" instead of the people they
      // came to talk to.
      if (!groupOrder.has(entry.command.group)) groupOrder.set(entry.command.group, entry.index)
    }

    return scored
      .sort(
        (a, b) =>
          (best.get(a.command.group) ?? 0) - (best.get(b.command.group) ?? 0) ||
          (groupOrder.get(a.command.group) ?? 0) - (groupOrder.get(b.command.group) ?? 0) ||
          a.score - b.score ||
          a.index - b.index,
      )
      .map((entry) => entry.command)
      .slice(0, 40)
  }, [commands, query])

  useEffect(() => setActive(0), [query])

  useEffect(() => {
    if (!open) return
    restoreTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setQuery("")
    setActive(0)
    // The input is rendered by this same commit; focus after it exists.
    const id = requestAnimationFrame(() => inputRef.current?.focus())
    return () => {
      cancelAnimationFrame(id)
      restoreTo.current?.focus?.()
    }
  }, [open])

  // Keep the highlighted row on screen without smooth-scrolling the list under
  // a held-down arrow key.
  useEffect(() => {
    if (!open) return
    const node = listRef.current?.children[active]
    if (node instanceof HTMLElement) node.scrollIntoView({ block: "nearest" })
  }, [active, open, results.length])

  /*
   * The entrance. Short, and only ever *from* an offset — a dropped frame or a
   * killed tween leaves a readable palette rather than an invisible one. Both
   * durations come from `dur()`, so reduced motion collapses this to nothing
   * without a second code path.
   */
  useGSAP(
    () => {
      if (!open) return
      gsap.from(".command-palette__panel", {
        y: -10,
        scale: 0.985,
        autoAlpha: 0,
        duration: dur("fast"),
        ease: ease("entrance"),
      })
      gsap.from(".command-palette__scrim", { autoAlpha: 0, duration: dur("fast"), ease: ease("entrance") })
    },
    { dependencies: [open], scope: rootRef },
  )

  if (!open) return null

  const choose = (command: Command | undefined) => {
    if (!command) return
    onClose()
    command.run()
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    switch (event.key) {
      case "Escape":
        event.preventDefault()
        onClose()
        break
      case "ArrowDown":
        event.preventDefault()
        setActive((prev) => (results.length === 0 ? 0 : (prev + 1) % results.length))
        break
      case "ArrowUp":
        event.preventDefault()
        setActive((prev) => (results.length === 0 ? 0 : (prev - 1 + results.length) % results.length))
        break
      case "Home":
        event.preventDefault()
        setActive(0)
        break
      case "End":
        event.preventDefault()
        setActive(Math.max(0, results.length - 1))
        break
      case "Enter":
        event.preventDefault()
        choose(results[active])
        break
      default:
        break
    }
  }

  let lastGroup: string | null = null

  return (
    <div className="command-palette" ref={rootRef} role="presentation">
      {/* Clicking away closes. A palette you cannot dismiss by looking away
          from it is a modal, and this is not important enough to be one. */}
      <div className="command-palette__scrim" onClick={onClose} aria-hidden="true" />

      <div
        className="command-palette__panel"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKeyDown}
      >
        <div className="command-palette__search">
          <Icon name="search" size={16} />
          <label className="sr-only" htmlFor="command-palette-input">
            Search commands and teammates
          </label>
          <input
            id="command-palette-input"
            ref={inputRef}
            className="command-palette__input"
            value={query}
            placeholder="Go to a teammate, a section, or an action…"
            autoComplete="off"
            spellCheck={false}
            role="combobox"
            aria-expanded
            aria-controls="command-palette-list"
            aria-activedescendant={results[active] ? `command-${results[active].id}` : undefined}
            onChange={(event) => setQuery(event.target.value)}
          />
          <kbd className="command-palette__esc">Esc</kbd>
        </div>

        {results.length === 0 ? (
          <p className="command-palette__empty">Nothing matches “{query}”.</p>
        ) : (
          <ul className="command-palette__list" id="command-palette-list" role="listbox" ref={listRef}>
            {results.map((command, index) => {
              const showGroup = command.group !== lastGroup
              lastGroup = command.group
              return (
                <li
                  key={command.id}
                  id={`command-${command.id}`}
                  role="option"
                  aria-selected={index === active}
                  className={cx("command-palette__item", index === active && "command-palette__item--active")}
                  /* Pointer selects rather than activates: moving the mouse
                     across the list should not fire anything, and a click
                     should not depend on which row the keyboard was on. */
                  onMouseMove={() => setActive(index)}
                  onClick={() => choose(command)}
                >
                  {showGroup ? <span className="command-palette__group">{command.group}</span> : null}
                  <span className="command-palette__row">
                    {command.glyph ? (
                      <span className="command-palette__glyph" aria-hidden="true">
                        <Icon name={command.glyph} size={15} />
                      </span>
                    ) : null}
                    <span className="command-palette__label">{command.label}</span>
                    {command.detail ? <span className="command-palette__detail">{command.detail}</span> : null}
                    {command.shortcut ? <kbd className="command-palette__kbd">{command.shortcut}</kbd> : null}
                  </span>
                </li>
              )
            })}
          </ul>
        )}

        <footer className="command-palette__footer">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> move
          </span>
          <span>
            <kbd>↵</kbd> open
          </span>
          <span>
            <kbd>Esc</kbd> close
          </span>
        </footer>
      </div>
    </div>
  )
}
