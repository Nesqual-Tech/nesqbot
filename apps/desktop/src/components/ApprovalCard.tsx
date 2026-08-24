import { useEffect, useRef, useState } from "react"
import { approvalExecutionOutcome, parseApprovalPayload, type HeldActionInPlainWords } from "@nesqbot/protocol"
import { riskDescriptions, riskLabels, type RiskClass } from "@nesqbot/ui"
import { errorMessage, isApiError } from "../api/client"
import { consequenceOf } from "../lib/approvals"
import { cx, prettyJson, relativeTime } from "../lib/format"
import { Icon } from "./Icon"
import { Markdown } from "./Markdown"
import type { OptimisticDecision } from "../hooks/useApprovals"
import type { Approval, ApprovalContinuation, ApprovalDecisionResult, Bot, ExecutionResult } from "../types"

export interface ApprovalCardProps {
  approval: Approval
  bot?: Bot
  /** True while this card's decision is in flight with the server. */
  deciding: boolean
  /** The decision this client is showing ahead of the server, if any. */
  optimistic?: OptimisticDecision
  highlight?: boolean
  onDecide: (id: string, decision: "approved" | "rejected", note?: string) => Promise<ApprovalDecisionResult>
}

/**
 * The held desktop action, in words.
 *
 * Every string here was composed on the server by
 * `orchestrator.held_action_in_plain_words`, from the arguments that actually
 * reached the chokepoint. Nothing is rebuilt client-side, because a second
 * renderer is a second dialect and the whole point of the block is that the
 * card, the chat reply and the push notification say the same sentence.
 *
 * Two things this component will not do, both of which read more nicely and
 * both of which would be lies:
 *
 * * it never puts the held action in the past tense. `plain.intent` is
 *   *click "Message"*, present, because it has not happened;
 * * it never labels the message as what *will* be sent. The heading says what
 *   was typed, because that is what is known — the send is the thing being
 *   decided on.
 */
function PlainDetail({ plain }: { plain: HeldActionInPlainWords }) {
  return (
    <div className="approval__detail approval__plain">
      <dl className="kv">
        <dt>Wants to</dt>
        <dd className="approval__plain-intent">{plain.intent}</dd>
        {plain.place ? (
          <>
            <dt>On</dt>
            <dd>{plain.place}</dd>
          </>
        ) : null}
      </dl>

      {plain.message ? (
        <>
          <div className="approval__detail-label">The message it typed into {plain.message.into}</div>
          {/*
            A blockquote, not a <pre class="code-block">. The text is Romanian
            outreach copy a person wrote through a bot — it should wrap, use the
            reading typeface and be legible at a glance, which is the opposite
            of what a monospace scroll box does for prose.
          */}
          <blockquote className="approval__message">
            {plain.message.text}
            {plain.message.truncated ? <span className="approval__message-clip">…truncated for storage</span> : null}
          </blockquote>
        </>
      ) : null}

      {plain.leading_up_to.length > 0 ? (
        <>
          <div className="approval__detail-label">How it got here</div>
          <ol className="approval__steps approval__steps--plain">
            {plain.leading_up_to.map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ol>
        </>
      ) : null}
    </div>
  )
}

/**
 * Renders the held action. `parseApprovalPayload` narrows the JSONB column to
 * the real discriminated union, so every kind gets a proper rendering — the
 * raw-JSON toggle below is a fallback for rows that predate the discriminator,
 * not the primary view for three of the four kinds.
 */
function PayloadDetail({ approval }: { approval: Approval }) {
  const payload = parseApprovalPayload(approval.payload)
  if (!payload) return null

  switch (payload.kind) {
    case "connector_action":
      return (
        <div className="approval__detail">
          <dl className="kv">
            <dt>Connector</dt>
            <dd>
              <code>{payload.connector_id}</code>
            </dd>
            <dt>Action</dt>
            <dd>
              <code>{payload.action}</code>
            </dd>
          </dl>
          {Object.keys(payload.input).length > 0 ? (
            <>
              <div className="approval__detail-label">Inputs</div>
              <dl className="kv kv--wrap">
                {Object.entries(payload.input).map(([key, value]) => (
                  <div className="kv__pair" key={key}>
                    <dt>{key}</dt>
                    <dd>{typeof value === "string" ? value : prettyJson(value)}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : null}
          {payload.draft ? (
            <>
              <div className="approval__detail-label">Draft</div>
              <pre className="code-block">{payload.draft}</pre>
            </>
          ) : null}
        </div>
      )

    case "mcp_tool":
      return (
        <div className="approval__detail">
          <dl className="kv">
            <dt>Server</dt>
            <dd>
              <code>{payload.mcp_id}</code>
            </dd>
            <dt>Tool</dt>
            <dd>
              <code>{payload.tool}</code>
            </dd>
          </dl>
          <div className="approval__detail-label">Arguments</div>
          <pre className="code-block">{prettyJson(payload.arguments)}</pre>
        </div>
      )

    case "desktop_steps":
      /*
       * The owner's complaint, in full: *"on approval i would like to see what
       * the agent is trying to do, the message it's trying to send, not
       * payloads."*
       *
       * What this arm used to render was `browser_click` in a <code> tag with
       * `e358` beside it. The server now writes a `plain` block from the same
       * vocabulary the chat reply uses, so the card says
       * *click "Message" on linkedin.com/in/andrei-pop* and shows the Romanian
       * text underneath it. The raw steps are still one click further in, under
       * "Show raw payload", where somebody debugging can have them.
       *
       * The fallback is not dead code: rows held before this shipped have no
       * `plain`, and they still have to render as something.
       */
      return payload.plain ? (
        <PlainDetail plain={payload.plain} />
      ) : (
        <div className="approval__detail">
          <div className="approval__detail-label">
            {payload.steps.length} desktop step{payload.steps.length === 1 ? "" : "s"}
            {payload.profile ? ` · ${payload.profile}` : ""}
          </div>
          <ol className="approval__steps">
            {payload.steps.map((step, index) => (
              <li key={index}>
                <code>{step.action}</code>
                {typeof step.x === "number" ? (
                  <span className="muted">
                    {" "}
                    {step.x},{step.y ?? 0}
                  </span>
                ) : null}
                {step.text ? <span className="muted"> “{step.text}”</span> : null}
                {step.keys && step.keys.length > 0 ? <span className="muted"> {step.keys.join("+")}</span> : null}
              </li>
            ))}
          </ol>
        </div>
      )

    case "message_only":
      return (
        <div className="approval__detail">
          {payload.to ? (
            <dl className="kv">
              <dt>To</dt>
              <dd>{payload.to}</dd>
            </dl>
          ) : null}
          <div className="approval__detail-label">Draft</div>
          <pre className="code-block">{payload.draft}</pre>
        </div>
      )

    default:
      return null
  }
}

/**
 * The three-armed `execution` envelope, rendered as three different things.
 *
 * `approvalExecutionOutcome` exists because the obvious `execution.ok ? … : …`
 * is wrong: a **rejection that let the task carry on** has no `ok` at all, and
 * branching on truthiness rendered a perfectly ordinary "no thanks" as an
 * execution failure with an empty error box under it. That third arm is the
 * common shape now that a decision resumes the parked run.
 */
function ExecutionBlock({ execution }: { execution: ExecutionResult }) {
  const outcome = approvalExecutionOutcome(execution)
  if (outcome === "not-executed") return null

  const ok = outcome === "ran"
  return (
    <div className={cx("execution", ok ? "execution--ok" : "execution--error")} role="status">
      <div className="execution__title">{ok ? "Executed" : "Execution failed"}</div>
      <pre className="code-block">
        {ok
          ? prettyJson((execution as { result?: unknown }).result ?? "Done.")
          : (execution as { error: string }).error}
      </pre>
    </div>
  )
}

/**
 * The half of the answer this product is actually about: the task moved on.
 *
 * Deciding does not only record a decision — the API resumes the parked run
 * through the same path as the takeover Continue button, and rides the result
 * back as `execution.continuation`. Until now the only evidence of that on
 * screen was a toast reading "Approved", which says nothing about the agent.
 *
 * `continued: false` is **not** a failure. The resume is idempotent via a
 * conditional status claim, so a second press loses the race and says so.
 */
function ContinuationBlock({ continuation, botName }: { continuation: ApprovalContinuation; botName: string }) {
  const who = botName || "The bot"
  const line = continuation.error
    ? `${who} could not pick the task back up: ${continuation.error}`
    : continuation.continued
      ? `${who} picked the task back up.`
      : "That task was already running again."

  return (
    <p className={cx("approval__continuation", continuation.error && "approval__continuation--error")} role="status">
      <Icon name={continuation.error ? "alert" : "repeat"} size={13} />
      <span>{line}</span>
      {continuation.outcome && !continuation.error ? (
        <span className="approval__continuation-outcome">{continuation.outcome}</span>
      ) : null}
    </p>
  )
}

export function ApprovalCard({ approval, bot, deciding, optimistic, highlight, onDecide }: ApprovalCardProps) {
  const [note, setNote] = useState("")
  const [showRaw, setShowRaw] = useState(false)
  const [execution, setExecution] = useState<ExecutionResult | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  /*
   * Collapsed until asked for.
   *
   * Every card used to render every field it had — the full draft, the input
   * table, the note box and both buttons — so four pending approvals was four
   * screens of scrolling and the queue could not be read as a queue. A person
   * arriving at this panel is triaging: what is waiting, how bad is it, which
   * one first. That is the summary. The payload is the second question, and it
   * gets a click.
   *
   * The one the app navigated you to opens itself, because you did not come
   * here to triage — you came for that one.
   */
  const [open, setOpen] = useState(Boolean(highlight))
  const ref = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!highlight) return
    setOpen(true)
    ref.current?.scrollIntoView({ block: "center", behavior: "auto" })
  }, [highlight])

  /*
   * `status` is read straight off the prop rather than mirrored into state.
   *
   * It used to be a `useState` seeded from the prop and re-synced by an effect,
   * which put the card one render behind every change — and with the decision
   * now applied optimistically in `useApprovals`, one render behind is exactly
   * the latency this pass exists to remove. The row in the hook is the single
   * account of what this approval's status is; the card renders it.
   */
  const status = approval.status
  const pending = status === "pending"
  const risk = approval.risk
  const known = risk as RiskClass
  const consequence = consequenceOf(approval)

  /*
   * Pressed, and the server has not answered.
   *
   * Not the same as `deciding` alone: what the strip below says depends on
   * which way it went, and only the optimistic record knows that. A decision
   * that has already been rolled back (`lost`) is not settling — it is over.
   */
  const settling = deciding && optimistic !== undefined && !optimistic.lost
  const lostRace = optimistic?.lost === true

  const decide = async (decision: "approved" | "rejected") => {
    setFailure(null)
    setExecution(null)
    try {
      const result = await onDecide(approval.id, decision, note)
      if (result.execution) setExecution(result.execution)
    } catch (err) {
      /*
       * The row has already been rolled back by the hook, so all this has to do
       * is say why — and a 409 earns its own sentence. "Someone answered this
       * before you" is a different fact from "the request failed" and leads to
       * a different next move, and an optimistic UI that quietly reverts
       * without explaining itself is the exact failure mode people distrust.
       */
      setFailure(
        isApiError(err) && err.isConflict
          ? "Already decided somewhere else — your decision was not applied. The queue has been refreshed with the real answer."
          : errorMessage(err),
      )
    }
  }

  const continuation = execution?.continuation ?? null
  const granted = execution?.standing_announcement ?? null
  const detailId = `approval-detail-${approval.id}`

  /*
   * The fold names what is behind it.
   *
   * "Show exactly what will run" was accurate about a payload and useless as an
   * invitation: the one thing the owner said they wanted to see on this screen
   * is the message, and a label that does not mention it is a label they have
   * no reason to press. When the hold carries no message the old wording is
   * still the honest one.
   */
  const heldPayload = parseApprovalPayload(approval.payload)
  const disclosureLabel =
    heldPayload?.kind === "desktop_steps" && heldPayload.plain?.message
      ? "Show the message and how it got here"
      : "Show exactly what will run"

  return (
    <article
      ref={ref}
      /*
       * `data-risk` on the article, not just on the chip.
       *
       * This is what turns the risk class from a label into the card's
       * appearance: the edge band, the risk word, the ring on the focused card
       * and the colour of the Approve button all read it in CSS. A `delete`
       * and a `draft` are now different objects at a glance, across the room,
       * which is the only way a queue of six is triageable.
       */
      data-risk={risk}
      data-status={status}
      className={cx(
        "card",
        "approval",
        open && "approval--open",
        highlight && "approval--highlight",
        !pending && "approval--decided",
        settling && "approval--settling",
      )}
      aria-label={`Approval: ${approval.title}`}
      aria-busy={settling}
    >
      <header className="approval__header">
        <div className="approval__lead">
          <div className="approval__risk-word" title={riskDescriptions[known] ?? undefined}>
            {riskLabels[known] ?? risk}
          </div>
          <h3 className="approval__title">{approval.title}</h3>
        </div>
        <div className="approval__meta">
          {bot ? (
            <span className="chip chip--muted">{bot.name}</span>
          ) : (
            <span className="chip chip--muted">{approval.bot_id.slice(0, 8)}</span>
          )}
          {pending ? null : <span className="chip chip--muted">{status}</span>}
          {approval.created_at ? <time className="approval__age">{relativeTime(approval.created_at)}</time> : null}
        </div>
      </header>

      {/*
        The consequence, and whether it can be taken back.
        See `lib/approvals.ts` for why this outranks everything else here.
      */}
      <div className="approval__consequence">
        <p className="approval__effect">{consequence.line}</p>
        <p className={cx("approval__undo", `approval__undo--${consequence.reversibility}`)}>
          <Icon name={consequence.reversibility === "reversible" ? "repeat" : "alert"} size={13} />
          {consequence.undo}
          {consequence.external ? <span className="approval__external">Leaves the company</span> : null}
        </p>
      </div>

      {/*
        Markdown, because this field is frequently the agent's own reply text:
        `services/orchestrator.py` fills it with `reply_text[:500]`, which is
        the same composed message the chat bubble gets — bold, backticks,
        numbered list and all. Everything else on this card is a payload
        rendered from structured data and stays literal.
      */}
      {approval.summary ? (
        <div className="approval__summary">
          <Markdown text={approval.summary} />
        </div>
      ) : null}

      <button
        type="button"
        className="approval__disclosure"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen((prev) => !prev)}
      >
        <Icon name={open ? "collapse" : "expand"} size={14} />
        {open ? "Hide the detail" : disclosureLabel}
      </button>

      {open ? (
        <div className="approval__reveal" id={detailId}>
          <PayloadDetail approval={approval} />

          <button
            type="button"
            className="btn btn--ghost btn--xs"
            aria-expanded={showRaw}
            onClick={() => setShowRaw((prev) => !prev)}
          >
            {showRaw ? "Hide raw payload" : "Show raw payload"}
          </button>
          {showRaw ? <pre className="code-block code-block--scroll">{prettyJson(approval.payload)}</pre> : null}
        </div>
      ) : null}

      {pending && !settling ? (
        <div className="approval__decide">
          {/*
            The note carries its label in the placeholder and an `sr-only`
            <label> for anyone who cannot see it. A stacked "NOTE (OPTIONAL)"
            eyebrow cost forty vertical pixels on every card in the queue to
            restate a word already sitting inside the field — and the whole
            point of this pass was that a queue of six should be readable
            without scrolling six times.
          */}
          <label className="approval__note">
            <span className="sr-only">Note (optional)</span>
            <input
              className="input"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Add a note (optional)"
            />
          </label>
          {/*
            The primary action wears the *risk* colour rather than the brand
            accent. You are not "confirming" in the abstract, you are
            authorising a delete — and the button you are about to press should
            say which. Reject stays quiet: refusing is always safe, and a
            product that makes the safe choice look scary has its governance
            story backwards.

            Neither button carries a spinner any more, and neither disables
            itself. The decision lands the instant it is pressed, so there is
            nothing left for the button to be busy about — the whole control
            group is replaced by the strip below, which reports the part that
            genuinely takes time: the action running and the task resuming.
          */}
          <div className="approval__buttons">
            <button type="button" className="btn btn--risk" onClick={() => void decide("approved")}>
              {`Approve ${(riskLabels[known] ?? risk).toLowerCase()}`}
            </button>
            <button type="button" className="btn btn--ghost" onClick={() => void decide("rejected")}>
              Reject
            </button>
          </div>
        </div>
      ) : null}

      {/*
        The gap between "decided" and "done", said out loud.

        Approving executes the held action and then resumes the parked run, and
        both of those happen on the server after the click. Rather than hide
        that behind a disabled button, the card states the decision as settled —
        because it is — and reports the work as still going, because it is.
      */}
      {settling ? (
        <p className="approval__settling" role="status">
          <span className="approval__settling-pip" aria-hidden="true" />
          {optimistic?.decision === "approved"
            ? `Approved. Running the action and picking ${bot?.name ?? "the task"} back up…`
            : `Rejected. Telling ${bot?.name ?? "the bot"} the decision…`}
        </p>
      ) : null}

      {/*
        A standing permission was acquired by this decision, said here.

        The chat reply says it too, and both are needed: a hold raised by a
        routine has no parked run to carry a sentence into a reply, and a person
        who is not told their bot just stopped asking cannot revoke it. This is
        the announcement, not a confirmation — it appears exactly once, on the
        decision that created the rule.
      */}
      {granted ? (
        <p className="approval__granted" role="status">
          <Icon name="shield" size={13} />
          <span>{granted}</span>
        </p>
      ) : null}

      {execution ? <ExecutionBlock execution={execution} /> : null}
      {continuation ? <ContinuationBlock continuation={continuation} botName={bot?.name ?? ""} /> : null}

      {failure ? (
        <div className={cx("inline-error", lostRace && "inline-error--conflict")} role="alert">
          {failure}
        </div>
      ) : null}
    </article>
  )
}
