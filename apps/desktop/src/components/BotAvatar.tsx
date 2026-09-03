/**
 * A teammate, as one glyph.
 *
 * What this replaces: a tinted circle with the bot's initials in it, five
 * times down the sidebar. Colour was doing all of the identifying, which
 * fails twice — `Sales` and `Support` both render "S" over neighbouring
 * violets, and a column of identical circles reads as one control repeated
 * rather than as five different people.
 *
 * So each bot gets a silhouette as well as a colour: a hexagon, a triangle, a
 * square, a cloud or a circle. Shape survives 20px, greyscale and colour
 * blindness, and it is what people actually name a bot by once they have used
 * the app for a week. The slug→shape mapping lives in `@nesqbot/ui`
 * (`getBotShape`) so mobile can draw the same teammate the same way; only the
 * SVG is here.
 *
 * Filled, not stroked — deliberately unlike `Icon`, which is a stroked 24px
 * grid on `currentColor`. These are identity marks rather than affordances,
 * and a filled shape is what reads at sidebar size.
 */
import type { CSSProperties } from "react"
import { getBotColor, getBotShape, type BotShape } from "@nesqbot/ui"
import { cx } from "../lib/format"
import type { Bot } from "../types"

/**
 * Path data on a 24×24 grid, inset slightly so a shape with corners and a
 * shape without look the same weight next to each other.
 *
 * The cloud is three overlapping arcs closed along the bottom rather than a
 * `path` per lobe: one filled subpath, so it takes the tint the same way the
 * others do and has no seams where the lobes meet.
 */
const SHAPES: Record<BotShape, string> = {
  circle: "M12 2.5a9.5 9.5 0 1 0 0 19 9.5 9.5 0 0 0 0-19Z",
  square: "M6 3h12a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3Z",
  triangle: "M12 3.2 21.5 20H2.5L12 3.2Z",
  hexagon: "M12 2.2 20.5 7v10L12 21.8 3.5 17V7L12 2.2Z",
  cloud:
    "M7.5 19.5a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18.2 11a4.25 4.25 0 0 1-.7 8.5H7.5Z",
}

export interface BotAvatarProps {
  bot: Pick<Bot, "slug" | "name">
  /** Rendered size in px. The grid is 24, so anything scales cleanly. */
  size?: number
  className?: string
  /**
   * Name the avatar for a screen reader. Off by default: next to the bot's
   * own name — which is the usual case — this would read the name twice.
   */
  labelled?: boolean
}

export function BotAvatar({ bot, size = 28, className, labelled = false }: BotAvatarProps) {
  const shape = getBotShape(bot.slug)
  const color = getBotColor(bot.slug)
  return (
    <svg
      className={cx("bot-avatar", className)}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      role={labelled ? "img" : undefined}
      aria-hidden={labelled ? undefined : true}
      focusable="false"
    >
      {labelled ? <title>{bot.name}</title> : null}
      <path d={SHAPES[shape]} fill={color} />
    </svg>
  )
}

export interface BotAvatarStackProps {
  bots: Pick<Bot, "id" | "slug" | "name">[]
  size?: number
  className?: string
}

/**
 * A group thread, as one glyph.
 *
 * Three at most, overlapped. A group of eight drawn as eight marks is a
 * smear at sidebar width, and the row already spells out the names underneath
 * — so the stack's job is only to say "this is more than one person", which
 * three shapes do as well as eight.
 */
export function BotAvatarStack({ bots, size = 28, className }: BotAvatarStackProps) {
  const shown = bots.slice(0, 3)
  if (shown.length === 0) return null
  if (shown.length === 1) return <BotAvatar bot={shown[0]} size={size} className={className} />
  return (
    <span
      className={cx("bot-avatar-stack", className)}
      style={{ "--stack-size": `${size}px` } as CSSProperties}
      aria-hidden="true"
    >
      {shown.map((bot) => (
        <BotAvatar key={bot.id} bot={bot} size={size} />
      ))}
    </span>
  )
}
