/**
 * Generates the Nesq Bot application icon set.
 *
 *   node src-tauri/icons/generate.mjs
 *
 * Emits PNG (deflate), ICO (BMP entries plus a PNG 256) and ICNS (PNG
 * payloads) using nothing but `node:zlib` - there is no image tooling on the
 * build box and no binary source asset to keep in sync.
 *
 * The mark
 * --------
 * The geometry is the real Nesqual mark, read at generation time out of
 * `packages/ui/src/logo.ts`, where it is a two-polygon trace measured to
 * within a pixel of the original artwork. The previous version of this script
 * drew its own three-stroke "N" instead - a vertical, a diagonal and another
 * vertical - which is not the mark: the real one is a folded ribbon with a
 * deliberate gap between its two diagonals, and the halves are different
 * colours. Colours likewise come from `packages/ui`, not from constants here.
 *
 * Why the small sizes are drawn differently
 * ---------------------------------------
 * The first version of this icon was a thin `#8499da` monogram on a `#0b0d1a`
 * square. On a dark taskbar that is a mid-tone figure on a near-black ground
 * on a near-black background: at 16 and 32 px it read as an empty tile. Two
 * things fix it, and both are per-size decisions rather than one drawing
 * scaled down:
 *
 *   1. The ink half of the mark is white, which is what the real artwork uses
 *      and what carries the silhouette against a dark taskbar. The tile can
 *      disappear into the background; the glyph cannot.
 *   2. At 24 px and below the accent half is drawn white as well. Two mid-tone
 *      pixels next to four white ones do not read as "the other half of the
 *      letter", they read as a smudge - so below that threshold the mark goes
 *      monochrome and keeps its shape instead of its palette. Between 32 and
 *      48 px the accent is lifted to `brandMark.300`, which holds its own
 *      against the white at small stroke widths; from 64 px up it is the real
 *      sampled `#8499da`.
 *
 * The mark also fills far more of the canvas than it used to (70-80% of the
 * height against roughly 47%), which is most of what makes it survive the
 * downscale.
 */
import { deflateSync } from "node:zlib"
import { mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const OUT_DIR = dirname(fileURLToPath(import.meta.url))
const UI_SRC = join(OUT_DIR, "..", "..", "..", "..", "packages", "ui", "src")

/* ------------------------------------------------- design system, at build */

function read(name) {
  return readFileSync(join(UI_SRC, name), "utf8")
}

function extract(text, pattern, what) {
  const match = text.match(pattern)
  if (!match) throw new Error(`could not read ${what} out of packages/ui - has the source moved?`)
  return match[1]
}

function rgb(hex) {
  const value = hex.replace("#", "")
  return [parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16)]
}

const logoSrc = read("logo.ts")
const brandSrc = read("brand.ts")
const tokensSrc = read("tokens.ts")

const NAVY = rgb(extract(tokensSrc, /brandNavy\s*=\s*"(#[0-9a-fA-F]{6})"/, "the brand navy"))
const INK = rgb(extract(brandSrc, /BRAND_INK_HEX\s*=\s*"(#[0-9a-fA-F]{6})"/, "the ink colour"))
const ACCENT = rgb(extract(brandSrc, /BRAND_MARK_HEX\s*=\s*"(#[0-9a-fA-F]{6})"/, "the mark colour"))
// brandMark.300, the step above the sampled anchor. Used only at 32 and 48 px.
const ACCENT_LIFTED = rgb(extract(tokensSrc, /300:\s*"(#[0-9a-fA-F]{6})"/, "brandMark.300"))
const BORDER = rgb(extract(tokensSrc, /border:\s*"(#[0-9a-fA-F]{6})"/, "the dark border colour"))

/* ------------------------------------------------------------------ shape */

const MARK_W = Number(extract(logoSrc, /NESQUAL_MARK_WIDTH\s*=\s*(\d+)/, "the mark width"))
const MARK_H = Number(extract(logoSrc, /NESQUAL_MARK_HEIGHT\s*=\s*(\d+)/, "the mark height"))

/**
 * The two closed polygons of absolute moveto/lineto that make up the mark.
 * Anything else in the path data means the artwork gained a curve and this
 * parser needs to grow, so refuse rather than draw a mangled mark.
 */
function polygon(d) {
  if (!/^[MLZ0-9 .,\-]+$/.test(d)) throw new Error(`unsupported path command in "${d}"`)
  const points = [...d.matchAll(/[ML]\s*(-?[\d.]+)[\s,]+(-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])])
  if (points.length < 3) throw new Error(`path "${d}" parsed to ${points.length} points`)
  return points
}

const POLY_INK = polygon(extract(logoSrc, /ink:\s*"([^"]+)"/, "the mark's ink path"))
const POLY_ACCENT = polygon(extract(logoSrc, /accent:\s*"([^"]+)"/, "the mark's accent path"))

/** Ray casting. Both polygons are simple, so a crossing count is enough. */
function inPolygon(points, x, y) {
  let inside = false
  for (let i = 0, j = points.length - 1; i < points.length; j = i, i += 1) {
    const [xi, yi] = points[i]
    const [xj, yj] = points[j]
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

/** Per-size drawing decisions. See the header comment for the reasoning. */
function styleFor(size) {
  if (size <= 24) return { markFraction: 0.8, radius: 0.16, accent: INK, border: false }
  if (size <= 48) return { markFraction: 0.76, radius: 0.2, accent: ACCENT_LIFTED, border: true }
  return { markFraction: 0.7, radius: 0.22, accent: ACCENT, border: true }
}

function insideRoundedSquare(x, y, radius) {
  const dx = Math.max(Math.abs(x - 0.5) - (0.5 - radius), 0)
  const dy = Math.max(Math.abs(y - 0.5) - (0.5 - radius), 0)
  return Math.hypot(dx, dy) <= radius
}

/** Supersampled RGBA raster of the icon at `size` px. */
function render(size) {
  const style = styleFor(size)
  const ss = size >= 256 ? 3 : 4
  const samples = ss * ss
  const out = Buffer.alloc(size * size * 4)

  // Mark placement, in unit coordinates: centred, `markFraction` of the height.
  const markH = style.markFraction
  const markW = markH * (MARK_W / MARK_H)
  const originX = (1 - markW) / 2
  const originY = (1 - markH) / 2
  const inset = style.border ? 1 / size : 0

  for (let py = 0; py < size; py += 1) {
    for (let px = 0; px < size; px += 1) {
      let sr = 0
      let sg = 0
      let sb = 0
      let covered = 0

      for (let sy = 0; sy < ss; sy += 1) {
        for (let sx = 0; sx < ss; sx += 1) {
          const x = (px + (sx + 0.5) / ss) / size
          const y = (py + (sy + 0.5) / ss) / size
          if (!insideRoundedSquare(x, y, style.radius)) continue

          // Mark-space coordinates for this sample.
          const mx = ((x - originX) / markW) * MARK_W
          const my = ((y - originY) / markH) * MARK_H

          let colour = NAVY
          if (inPolygon(POLY_INK, mx, my)) colour = INK
          else if (inPolygon(POLY_ACCENT, mx, my)) colour = style.accent
          else if (style.border && !insideRoundedSquare(x, y, style.radius - inset)) colour = BORDER

          sr += colour[0]
          sg += colour[1]
          sb += colour[2]
          covered += 1
        }
      }

      if (covered === 0) continue
      const offset = (py * size + px) * 4
      out[offset] = Math.round(sr / covered)
      out[offset + 1] = Math.round(sg / covered)
      out[offset + 2] = Math.round(sb / covered)
      out[offset + 3] = Math.round((covered / samples) * 255)
    }
  }

  return out
}

/* -------------------------------------------------------------------- png */

const CRC_TABLE = (() => {
  const table = new Int32Array(256)
  for (let n = 0; n < 256; n += 1) {
    let c = n
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c
  }
  return table
})()

function crc32(buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i += 1) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function chunk(type, data) {
  const head = Buffer.alloc(8)
  head.writeUInt32BE(data.length, 0)
  head.write(type, 4, "ascii")
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0)
  return Buffer.concat([head, data, crc])
}

function encodePng(size, rgba) {
  const stride = size * 4
  const raw = Buffer.alloc((stride + 1) * size)
  for (let y = 0; y < size; y += 1) {
    raw[y * (stride + 1)] = 0 // filter: none
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride)
  }

  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 6 // truecolour + alpha
  ihdr[10] = 0
  ihdr[11] = 0
  ihdr[12] = 0

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ])
}

/* -------------------------------------------------------------------- ico */

/** 32bpp bottom-up DIB plus the (all-zero) AND mask Windows still expects. */
function encodeDib(size, rgba) {
  const header = Buffer.alloc(40)
  const maskStride = Math.ceil(size / 32) * 4
  const pixels = Buffer.alloc(size * size * 4)
  const mask = Buffer.alloc(maskStride * size)

  header.writeUInt32LE(40, 0)
  header.writeInt32LE(size, 4)
  header.writeInt32LE(size * 2, 8) // colour rows + mask rows
  header.writeUInt16LE(1, 12)
  header.writeUInt16LE(32, 14)
  header.writeUInt32LE(0, 16)
  header.writeUInt32LE(pixels.length + mask.length, 20)

  for (let y = 0; y < size; y += 1) {
    const src = (size - 1 - y) * size * 4
    for (let x = 0; x < size; x += 1) {
      const from = src + x * 4
      const to = (y * size + x) * 4
      pixels[to] = rgba[from + 2]
      pixels[to + 1] = rgba[from + 1]
      pixels[to + 2] = rgba[from]
      pixels[to + 3] = rgba[from + 3]
    }
  }

  return Buffer.concat([header, pixels, mask])
}

function encodeIco(entries) {
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0)
  header.writeUInt16LE(1, 2) // type: icon
  header.writeUInt16LE(entries.length, 4)

  const directory = Buffer.alloc(16 * entries.length)
  let offset = header.length + directory.length
  const payloads = []

  entries.forEach((entry, index) => {
    const at = index * 16
    directory[at] = entry.size >= 256 ? 0 : entry.size
    directory[at + 1] = entry.size >= 256 ? 0 : entry.size
    directory[at + 2] = 0 // palette
    directory[at + 3] = 0 // reserved
    directory.writeUInt16LE(1, at + 4) // planes
    directory.writeUInt16LE(32, at + 6) // bpp
    directory.writeUInt32LE(entry.data.length, at + 8)
    directory.writeUInt32LE(offset, at + 12)
    offset += entry.data.length
    payloads.push(entry.data)
  })

  return Buffer.concat([header, directory, ...payloads])
}

/* ------------------------------------------------------------------- icns */

function encodeIcns(entries) {
  const blocks = entries.map(({ type, data }) => {
    const head = Buffer.alloc(8)
    head.write(type, 0, "ascii")
    head.writeUInt32BE(data.length + 8, 4)
    return Buffer.concat([head, data])
  })
  const body = Buffer.concat(blocks)
  const head = Buffer.alloc(8)
  head.write("icns", 0, "ascii")
  head.writeUInt32BE(body.length + 8, 4)
  return Buffer.concat([head, body])
}

/* ------------------------------------------------------------------- main */

mkdirSync(OUT_DIR, { recursive: true })

const SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
const raster = new Map()
const pngs = new Map()
for (const size of SIZES) {
  const rgba = render(size)
  raster.set(size, rgba)
  pngs.set(size, encodePng(size, rgba))
}

const written = []
function write(name, data) {
  writeFileSync(join(OUT_DIR, name), data)
  written.push(`${name} - ${(data.length / 1024).toFixed(1)} kB`)
}

write("32x32.png", pngs.get(32))
write("128x128.png", pngs.get(128))
write("128x128@2x.png", pngs.get(256))
write("icon.png", pngs.get(512))

// Largest first on purpose: `tauri-codegen` takes `entries()[0]` from the ICO
// for the Windows default window icon, and Windows itself picks by size from
// the embedded RT_GROUP_ICON regardless of order.
//
// 24 px is in here because Windows uses it for the small-icon slot on some
// scale factors, and if it is missing the shell downscales the 32 and undoes
// the small-size treatment above.
write(
  "icon.ico",
  encodeIco([
    { size: 256, data: pngs.get(256) },
    { size: 128, data: encodeDib(128, raster.get(128)) },
    { size: 64, data: encodeDib(64, raster.get(64)) },
    { size: 48, data: encodeDib(48, raster.get(48)) },
    { size: 32, data: encodeDib(32, raster.get(32)) },
    { size: 24, data: encodeDib(24, raster.get(24)) },
    { size: 16, data: encodeDib(16, raster.get(16)) },
  ]),
)

write(
  "icon.icns",
  encodeIcns([
    { type: "icp4", data: pngs.get(16) },
    { type: "icp5", data: pngs.get(32) },
    { type: "ic07", data: pngs.get(128) },
    { type: "ic08", data: pngs.get(256) },
    { type: "ic09", data: pngs.get(512) },
    { type: "ic11", data: pngs.get(32) },
    { type: "ic12", data: pngs.get(64) },
    { type: "ic13", data: pngs.get(256) },
    { type: "ic14", data: pngs.get(512) },
  ]),
)

console.log(written.join("\n"))
