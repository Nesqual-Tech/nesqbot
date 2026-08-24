/**
 * "I need you for a second."
 *
 * The agent drove the task as far as it could, hit a login it cannot pass, and
 * parked. This is the card that says so, and it sits directly above the screen
 * the person is about to sign in on — not in a toast, not in a notification
 * centre, not behind a tab. If this is missable the whole feature is missable.
 *
 * Three things it has to get right:
 *
 *  1. **Say what happened and what is wanted.** `reason` is the bot's own
 *     sentence; `what_you_need` is the instruction. Both come straight from the
 *     API and neither is paraphrased here.
 *  2. **Send Continue once.** The button disables on press, but the real
 *     guarantee is the synchronous guard in `state/takeover` — see the note
 *     there. `resumed: false` comes back as "already going", never as an error.
 *  3. **Never be a credential field.** The note is for the transcript
 *     ("logged in as avery@…"), it is not persisted anywhere, and it says so.
 *     Passwords are typed on the bot's desktop, which is the point of the pane
 *     underneath.
 */
import { useCallback, useId, useRef, useState } from "react"
import { useTakeover } from "../state/takeover"
import { useToast } from "../state/AppState"
import { cx, relativeTime } from "../lib/format"
import { dur, ease, gsap, stagger, useGSAP } from "../lib/motion"
import type { TakeoverRequest } from "../lib/takeover"
import { Icon } from "./Icon"
import { Markdown } from "./Markdown"
import { Spinner } from "./Spinner"

export interface TakeoverCardProps {
  request: TakeoverRequest
  /** Overrides the name carried on the event once the bot list has loaded. */
  botName?: string
  /**
   * Lifecycle state of that bot's desktop, when the caller knows it. Drives the
   * warning about the resume asymmetry — see `desktopWarning` below.
   */
  desktopState?: string | null
  /** Offered alongside the warning when the desktop is not up. */
  onStartDesktop?: () => void
  startingDesktop?: boolean
  /** True when the person already has the keyboard pointed at the bot's machine. */
  keyboardLive?: boolean
  /** Hand control of the pane to the person, if they do not have it yet. */
  onTakeControl?: () => void
  hasControl?: boolean
}

const NOTE_LIMIT = 200

export function TakeoverCard({
  request,
  botName,
  desktopState,
  onStartDesktop,
  startingDesktop = false,
  keyboardLive = false,
  onTakeControl,
  hasControl = false,
}: TakeoverCardProps) {
  const takeover = useTakeover()
  const toast = useToast()
  const noteId = useId()
  const root = useRef<HTMLElement | null>(null)
  const [note, setNote] = useState("")
  const [failure, setFailure] = useState<string | null>(null)

  const name = botName || request.botName
  const pending = takeover.isResuming(request.runId)

  /*
   * The asymmetry worth spelling out.
   *
   * Everywhere else in this app "the desktop is absent" means "boot it".
   * Resume deliberately does not: the entire value of the resume is the session
   * the human just authenticated, and restarting an ACI container takes the
   * filesystem — and therefore that session — with it. So a resume against a
   * stopped desktop stops and reports a signed-out browser rather than working
   * confidently on one. The person needs to know that *before* they press
   * Continue, not after.
   */
  const desktopUp = desktopState === "running" || desktopState === "suspended"
  const desktopWarning = desktopState != null && !desktopUp

  const onContinue = useCallback(async () => {
    setFailure(null)
    const outcome = await takeover.resume(request.runId, note)

    switch (outcome.kind) {
      case "resumed":
        toast.success(`${name} is carrying on`, outcome.result.message || "Picking the task back up where it stopped.")
        setNote("")
        break
      case "already-running":
        // Not a failure. The conditional status update lost the race, which
        // means the agent is already going.
        toast.info(`${name} is already going`, "That run had already been resumed.")
        setNote("")
        break
      case "ignored":
        // The synchronous guard caught a duplicate press. Nothing was sent and
        // nothing needs saying — the first press is still in flight.
        break
      case "failed":
        setFailure(outcome.error)
        toast.error(
          outcome.gone ? "That task is no longer waiting" : "Could not continue",
          outcome.code === "run_not_resumable"
            ? "The run has no saved agent state, so there is nothing to pick up. Send the task again."
            : outcome.error,
        )
        break
    }
  }, [takeover, request.runId, note, toast, name])

  /* ------------------------------------------------------------------ *
   * The handoff, as a movement
   *
   * This is the product's signature moment and the one place the design
   * system's `emphasized` curve is spent. The brief for it is "a colleague
   * turning to you", not "an error dialog": the card descends into the pane
   * from above the screen it is talking about, the badge settles with the one
   * overshoot in the app, and the sentences resolve in reading order.
   *
   * The hard rule it obeys: **motion must never delay the request.** So the
   * container's opacity resolves over `fast` while only its *position* takes
   * `slow`, and every child animates from a small offset rather than to one.
   * Worst case the whole card is legible in ~120ms and merely still moving; a
   * killed tween or a dropped frame leaves readable text, never a blank card.
   * Under reduced motion every duration token is already 0ms, so the same
   * timeline runs and simply lands on frame one.
   * ------------------------------------------------------------------ */
  useGSAP(
    () => {
      const tl = gsap.timeline()

      tl.from(root.current, { y: -16, scale: 0.985, duration: dur("slow"), ease: ease("entrance") }, 0)
        .from(root.current, { autoAlpha: 0, duration: dur("fast"), ease: ease("entrance") }, 0)
        // The one overshoot in the product. It is what makes this read as an
        // offer rather than an alarm.
        .from(".takeover__badge", { scale: 0.5, duration: dur("deliberate"), ease: ease("emphasized") }, stagger(0.04))
        // One comma-joined selector rather than an array of them: gsap.context
        // rewrites selector *text* against the scope, and a single string is
        // unambiguously rewritten and staggers in document order.
        .from(
          ".takeover__headings, .takeover__ask, .takeover__where, .takeover__note, .takeover__actions",
          {
            y: 6,
            autoAlpha: 0,
            duration: dur("fast"),
            ease: ease("entrance"),
            stagger: stagger(0.035),
          },
          stagger(0.06),
        )

      /*
       * No cleanup function here, deliberately, and it is worth saying why
       * because the obvious `return () => tl.kill()` is actively wrong.
       *
       * `useGSAP` reverts its context on unmount, and reverting a `from` tween
       * is what puts the element back the way it was found. `kill()` does not
       * revert -- it stops the timeline and drops it, so the context then has
       * nothing left to revert and the `from` tween's *start* values stay
       * welded to the element. This card spent an afternoon rendering as a
       * 235px tall block of nothing at `opacity: 0` for exactly that reason.
       * The context is the cleanup. Do not help it.
       */
    },
    // Empty dependencies on purpose: this is a mount choreography and it runs
    // once. `dur()` has already read the reduced-motion state by the time the
    // timeline is built, and somebody toggling the OS setting while a handoff
    // is on screen should not make the card replay its entrance at them.
    { scope: root },
  )

  /*
   * "Not now" leaves the way it came in.
   *
   * Created inside an event handler, so it goes through `contextSafe`: without
   * it this tween is born outside the hook's context, survives unmount and
   * fires `onComplete` against a dead component. With it, an unmount mid-flight
   * reverts the tween and the callback never runs — and the dismissal is a
   * state change the provider owns either way, so nothing is lost.
   */
  const { contextSafe } = useGSAP({ scope: root })

  const dismiss = contextSafe(() => {
    gsap.to(root.current, {
      y: -10,
      autoAlpha: 0,
      duration: dur("fast"),
      ease: ease("exit"),
      onComplete: () => takeover.dismiss(request.runId),
    })
  })

  return (
    <section
      ref={root}
      className={cx("takeover", pending && "takeover--pending")}
      role="alert"
      aria-labelledby={`${noteId}-title`}
    >
      <header className="takeover__head">
        <span className="takeover__badge" aria-hidden="true">
          <Icon name="user" size={15} />
        </span>
        <div className="takeover__headings">
          <h3 className="takeover__title" id={`${noteId}-title`}>
            {name} needs you
          </h3>
          {/*
            Both of these are the bot's own sentences, straight off the
            `takeover` event, and the same model that writes `**bold**` into
            its chat replies writes it here. Inline mode: emphasis, code spans
            and links render, block structure does not — a heading or a list
            inside a one-line alert would fight the card's layout, and neither
            field is ever meant to carry one.
          */}
          <p className="takeover__reason">
            <Markdown text={request.reason} inline />
          </p>
        </div>
        <span className="takeover__age">{relativeTime(request.raisedAt)}</span>
      </header>

      <p className="takeover__ask">
        <Markdown text={request.whatYouNeed} inline />
      </p>

      {/*
        Where the keyboard is, restated on the card itself. The input bar below
        the screen says the same thing, but somebody reading "sign in, then
        press continue" needs the answer to "sign in where?" in the same
        sentence, not two elements away.
      */}
      <div className={cx("takeover__where", keyboardLive && "takeover__where--live")}>
        <Icon name="keyboard" size={14} />
        {keyboardLive ? (
          <span>Your keyboard is going to {name}&rsquo;s machine. Type the sign-in on the screen below.</span>
        ) : hasControl ? (
          <span>You have control — click the screen below, then type.</span>
        ) : (
          <span>Take control of the screen below to type into it.</span>
        )}
        {!hasControl && onTakeControl ? (
          <button type="button" className="btn btn--ghost btn--xs takeover__where-action" onClick={onTakeControl}>
            Take control
          </button>
        ) : null}
      </div>

      {desktopWarning ? (
        <div className="takeover__warning" role="status">
          <Icon name="alert" size={14} />
          <span>
            This desktop is <strong>{desktopState}</strong>. Continuing will not restart it — and a restart would take
            the signed-in session with it. Start it and sign in again before continuing.
          </span>
          {onStartDesktop ? (
            <button
              type="button"
              className="btn btn--ghost btn--xs"
              onClick={onStartDesktop}
              disabled={startingDesktop}
            >
              {startingDesktop ? "Starting…" : "Start desktop"}
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="takeover__note">
        <label className="sr-only" htmlFor={noteId}>
          Optional note for the transcript
        </label>
        <input
          id={noteId}
          className="input"
          type="text"
          value={note}
          maxLength={NOTE_LIMIT}
          disabled={pending}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          data-1p-ignore="true"
          data-lpignore="true"
          placeholder="Optional note — “signed in as avery@…”"
          onChange={(event) => setNote(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || pending) return
            event.preventDefault()
            void onContinue()
          }}
        />
      </div>

      <div className="takeover__actions">
        <button
          type="button"
          className="btn btn--primary takeover__continue"
          onClick={() => void onContinue()}
          disabled={pending}
          aria-busy={pending}
          data-testid="takeover-continue"
          data-run-id={request.runId}
        >
          {pending ? (
            <Spinner inline label="Continuing…" />
          ) : (
            <>
              <Icon name="check" size={16} />
              I&rsquo;m done — continue
            </>
          )}
        </button>

        <button type="button" className="btn btn--ghost btn--sm" onClick={dismiss} disabled={pending}>
          Not now
        </button>

        <p className="takeover__fineprint">
          The note goes in the transcript. Type passwords on the screen below, never here — nothing typed there is
          captured or stored by this app.
        </p>
      </div>

      {failure ? (
        <div className="inline-error takeover__error" role="alert">
          <span>{failure}</span>
          <button type="button" className="btn btn--ghost btn--xs" onClick={() => setFailure(null)}>
            Dismiss
          </button>
        </div>
      ) : null}
    </section>
  )
}
