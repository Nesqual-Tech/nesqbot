/**
 * Honest progress for the slowest thing this product does.
 *
 * ## The wait
 *
 * A Bot Desktop is one hypervisor-isolated container group per bot. Bringing a
 * cold one up takes 30–90 seconds on ACI and can reach three minutes when the
 * image has to come down first. That is one to two orders of magnitude past the
 * one-to-two seconds people accept in an interactive product, and the app's
 * previous answer to all of it was a spinner captioned "Waiting for the first
 * frame…" — which is indistinguishable, for the whole three minutes, from a
 * hung app. It is the same complaint published reviewers make about the
 * competitor's computer use, and it is a complaint about *not being told*, not
 * about the seconds.
 *
 * ## Why a bar is allowed here and nowhere else
 *
 * `AgentActivity` deliberately has no progress bar: the agent does not know how
 * many steps a task will take, so a bar would have to be invented, and an
 * invented bar teaches people to distrust the honest numbers next to it.
 *
 * A container boot is the opposite case. The wait has a known distribution, the
 * server sends the elapsed seconds on every `desktop:starting` frame, and the
 * client can time its own request. So there is something real to draw. The
 * shape keeps it honest:
 *
 *  - the fill runs against the **worst** case, not the typical one, so it does
 *    not sit pinned at 99% through the part of the wait people find longest;
 *  - a tick marks the **typical** boot, so passing it reads as "slower than
 *    usual" rather than as "broken";
 *  - the fill never reaches the end, because a boot that has not finished has
 *    not finished;
 *  - and the caption names the stage, so the number is never the only thing
 *    said. "Pulling the desktop image" is worth more than any percentage.
 *
 * The stage boundaries are the real sequence a start goes through, not
 * decoration: allocation, image pull, session start. They are approximate and
 * the copy says so by never claiming to be a countdown ("usually ready in
 * about…", rounded up to five seconds).
 */

/** A normal cold start, in seconds. The tick on the bar. */
export const DESKTOP_TYPICAL_S = 90

/** The documented worst case, including an image pull. The bar's full width. */
export const DESKTOP_WORST_S = 180

/**
 * What is actually happening this many seconds into a start.
 *
 * Used when the server has not sent a `detail` of its own. A server-supplied
 * detail always wins — it knows, this only guesses from the clock.
 */
export function bootStage(seconds: number): string {
  if (seconds < 15) return "Allocating an isolated container group"
  if (seconds < 45) return "Pulling the desktop image — first boot only"
  if (seconds < DESKTOP_TYPICAL_S) return "Starting the desktop session"
  if (seconds < DESKTOP_WORST_S) return "Slower than usual — the image is still coming down"
  return "Well past a normal boot. It may not be coming up."
}

/** The same, for a shutdown, which is quick but not instant. */
export function stopStage(seconds: number): string {
  return seconds < 20 ? "Tearing the container group down" : "Still shutting down"
}

/**
 * How much longer, in words rather than as a countdown.
 *
 * Rounded up to five seconds on purpose: a per-second estimate implies a
 * precision nobody has, and watching an estimate tick down is a worse
 * experience than being told a range once.
 */
export function bootEta(seconds: number): string {
  const remaining = DESKTOP_TYPICAL_S - seconds
  if (remaining > 0) return `usually ready in about ${Math.ceil(remaining / 5) * 5}s`
  if (seconds < DESKTOP_WORST_S) return "past the usual wait — still going"
  return "this is longer than a start should take"
}

/** Fill fraction for the bar, as a percentage string, capped short of full. */
export function bootFillPercent(seconds: number): number {
  return Math.max(2, Math.min(97, (seconds / DESKTOP_WORST_S) * 100))
}

/** Where the "typical boot" tick sits, as a percentage of the bar. */
export const DESKTOP_TICK_PERCENT = (DESKTOP_TYPICAL_S / DESKTOP_WORST_S) * 100
