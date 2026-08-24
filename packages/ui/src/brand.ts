/**
 * Durable brand facts. The single source of truth for every brand string and
 * for the one colour the logo is actually made of.
 *
 * This module is a leaf: it imports nothing, so `tokens.ts` can take the mark
 * colour from here without a cycle. If you find yourself typing "Nesqual" into
 * a component, import from here instead.
 *
 * Provenance of `markHex`: the live logo was downloaded from nesqualtech.com
 * and its opaque pixels sampled. `#8499da` is the modal colour of the mark
 * (2831 fully-opaque pixels; the next most common value, `#8499dc`, has 205 and
 * is anti-aliasing). Everything else in the artwork is white. Before that
 * measurement the repo carried `#8499d9` — one step off in blue — and the whole
 * `brandMark` ramp was built around the wrong anchor.
 */

/** The accent colour of the "N" mark, sampled from the live artwork. */
export const BRAND_MARK_HEX = "#8499da"

/** The rest of the artwork. The logo is drawn for dark backgrounds. */
export const BRAND_INK_HEX = "#ffffff"

/**
 * Legal entity. This is the name that belongs on licences, invoices and the
 * installer's publisher field — not the trading name.
 */
export const companyLegalName = "Nesqual Tech SRL"

/** Positioning line from the company site. Sentence case; not the logo lockup. */
export const brandTagline = "We build software that ships"

/** The line set under the wordmark in the logo. Rendered in caps by the lockup. */
export const logoTagline = "Empowering Digital Frontiers"

/** What the company does, in one line. For about screens and installer blurbs. */
export const companyDescription = "An EU digital studio building custom software, AI integration and cybersecurity."

export const brand = {
  /** The application. */
  productName: "Nesq Bot",
  /** Trading name. Use this in UI chrome. */
  companyName: "Nesqual Tech",
  /** Legal entity. Use this in legal text and installer metadata. */
  companyLegalName,
  /** Company positioning line. */
  tagline: brandTagline,
  /** The line under the wordmark in the logo. */
  logoTagline,
  /** One-line description of the company. */
  description: companyDescription,
  /** The mark's accent colour, sampled from the artwork. */
  markHex: BRAND_MARK_HEX,
  /** The colour of the wordmark and tagline in the artwork. */
  inkHex: BRAND_INK_HEX,
  /** Primary domain. */
  domain: "nesqualtech.com",
  /** The scheme the logo was designed for. */
  nativeScheme: "dark",
} as const

export type Brand = typeof brand
