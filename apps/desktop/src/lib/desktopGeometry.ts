/**
 * Geometry for the Bot Desktop viewer — the mapping between a click on screen
 * and a pixel on the bot's 1440×900 X display.
 *
 * This is the part that breaks silently. The pane can now be any width from a
 * 340px side column to a maximised 2560px window, and the desktop is letterboxed
 * inside it rather than stretched. Get the layout right and the mapping wrong
 * and the product looks like it works while every click lands somewhere else,
 * so all of it lives here as pure functions with no DOM and no React.
 *
 * Two rules keep it honest:
 *
 *  1. **`fitSize` sizes the element; `containedRect` maps through it.** They are
 *     deliberately not the same function. `fitSize` floors to whole CSS pixels
 *     so the stage can never overflow its container by a subpixel and start a
 *     scrollbar; that floor makes the element's aspect ratio differ from the
 *     desktop's by up to one pixel. `containedRect` reproduces exactly what
 *     `object-fit: contain` paints inside whatever box it is actually given —
 *     fractional, unrounded — so the residue of that floor is absorbed instead
 *     of turning into drift at the bottom or right edge.
 *
 *  2. **Device pixel ratio is not part of this.** `getBoundingClientRect()`
 *     answers in CSS pixels, and the ratio (CSS px of painted image) → (desktop
 *     px) is the same whatever the display's scale factor is. The canvas backing
 *     store is sized to the *screenshot*, not to `devicePixelRatio`, and
 *     `pointToDesktop` is given the screenshot's own dimensions — so a HiDPI
 *     screen changes how sharp the image looks and nothing else. Multiplying
 *     anything here by `devicePixelRatio` is how you break it.
 */

export interface Size {
  width: number
  height: number
}

export interface Box {
  left: number
  top: number
  width: number
  height: number
}

export interface Point {
  x: number
  y: number
}

/**
 * What `infra/bot-desktop/entrypoint.sh` starts Xvfb at, used only until the
 * first screenshot arrives and reports the real geometry. Never used to map a
 * click: `pointToDesktop` is always handed the dimensions of the frame that is
 * actually on the canvas.
 */
export const DEFAULT_DESKTOP_SIZE: Size = { width: 1440, height: 900 }

export function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  if (max < min) return min
  return Math.min(max, Math.max(min, value))
}

/** A usable {width, height}, whatever partial or absent thing was passed in. */
export function normaliseSize(size?: Partial<Size> | null, fallback: Size = DEFAULT_DESKTOP_SIZE): Size {
  const width = size?.width
  const height = size?.height
  return {
    width: typeof width === "number" && Number.isFinite(width) && width > 0 ? Math.round(width) : fallback.width,
    height: typeof height === "number" && Number.isFinite(height) && height > 0 ? Math.round(height) : fallback.height,
  }
}

/**
 * The largest whole-pixel box with `natural`'s aspect ratio that fits inside
 * `available`. Upscales as well as downscales — a maximised pane on a 2560px
 * monitor should render the desktop bigger than 1:1, because legibility is the
 * entire point of maximising it.
 *
 * Floors, so the result is never a subpixel wider than the container.
 */
export function fitSize(available: Size, natural: Size): Size {
  const availW = Math.max(1, available.width)
  const availH = Math.max(1, available.height)
  const natW = Math.max(1, natural.width)
  const natH = Math.max(1, natural.height)
  const scale = Math.min(availW / natW, availH / natH)
  return {
    width: Math.max(1, Math.floor(natW * scale)),
    height: Math.max(1, Math.floor(natH * scale)),
  }
}

/**
 * The sub-rectangle of `box` that an `object-fit: contain` image of `natural`
 * proportions actually occupies — i.e. `box` minus the letterbox bars.
 *
 * Fractional on purpose. Rounding here is what puts a click a pixel or two out
 * near the edges of a large stage.
 */
export function containedRect(box: Box, natural: Size): Box {
  const boxW = Math.max(0, box.width)
  const boxH = Math.max(0, box.height)
  const natW = Math.max(1, natural.width)
  const natH = Math.max(1, natural.height)
  if (boxW === 0 || boxH === 0) return { left: box.left, top: box.top, width: 0, height: 0 }

  const scale = Math.min(boxW / natW, boxH / natH)
  const width = natW * scale
  const height = natH * scale
  return {
    left: box.left + (boxW - width) / 2,
    top: box.top + (boxH - height) / 2,
    width,
    height,
  }
}

/**
 * Viewport coordinates (a `MouseEvent`'s `clientX`/`clientY`) → a pixel on the
 * bot's display.
 *
 * `box` is the stage element's `getBoundingClientRect()`; `natural` is the size
 * of the frame being painted. Returns `null` when the point is in a letterbox
 * bar rather than on the desktop, so a click on the background does nothing
 * instead of being clamped onto an edge the user never aimed at. Half a pixel
 * of tolerance keeps the outermost row and column clickable.
 */
export function pointToDesktop(clientX: number, clientY: number, box: Box, natural: Size): Point | null {
  const content = containedRect(box, natural)
  if (content.width <= 0 || content.height <= 0) return null

  const natW = Math.max(1, natural.width)
  const natH = Math.max(1, natural.height)
  const x = ((clientX - content.left) / content.width) * natW
  const y = ((clientY - content.top) / content.height) * natH

  if (x < -0.5 || y < -0.5 || x > natW + 0.5 || y > natH + 0.5) return null
  // `floor`, not `round`. `x` is a continuous position inside the image and the
  // pixel under it is the one it falls in — rounding hands back the pixel to
  // the right of the pointer for the whole right-hand half of every pixel, i.e.
  // a systematic half-pixel bias down and to the right. At the 30% scale of a
  // docked pane that half pixel of screen is ~1.6 desktop pixels, which is the
  // difference between a checkbox and its label.
  return {
    x: Math.floor(clamp(x, 0, natW - 0.0001)),
    y: Math.floor(clamp(y, 0, natH - 0.0001)),
  }
}

/**
 * The inverse: where a desktop pixel lands on screen, in viewport coordinates.
 *
 * Used to prove `pointToDesktop` round-trips at every stage size, and to park
 * the click marker on the exact spot that was sent.
 */
export function desktopToClient(point: Point, box: Box, natural: Size): Point {
  const content = containedRect(box, natural)
  const natW = Math.max(1, natural.width)
  const natH = Math.max(1, natural.height)
  return {
    x: content.left + ((point.x + 0.5) / natW) * content.width,
    y: content.top + ((point.y + 0.5) / natH) * content.height,
  }
}

/** A desktop pixel as a percentage of the stage, for positioning an overlay. */
export function desktopToPercent(point: Point, natural: Size): { left: string; top: string } {
  const natW = Math.max(1, natural.width)
  const natH = Math.max(1, natural.height)
  return {
    left: `${clamp(((point.x + 0.5) / natW) * 100, 0, 100)}%`,
    top: `${clamp(((point.y + 0.5) / natH) * 100, 0, 100)}%`,
  }
}

/** Rendered scale as a percentage, for the status readout ("74%", "100%"). */
export function scalePercent(stage: Size, natural: Size): number {
  const natW = Math.max(1, natural.width)
  return Math.round((stage.width / natW) * 100)
}
