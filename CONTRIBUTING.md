# Contributing to Nesq Bot

Thank you for considering it. This document says what we accept, what we do not,
how decisions get made, and what you are agreeing to when you send a patch. It
is deliberately explicit — improvising governance under pressure goes badly for
everyone.

## Before anything else: the licence and the CLA

Nesq Bot is **source-available**, not open source. It is licensed under the
Functional Source License 1.1 with an Apache 2.0 future licence
(`FSL-1.1-ALv2`) — see [LICENSE](LICENSE).

**Every contribution requires a signed Contributor Licence Agreement (CLA)**
before it can be merged. The CLA assigns copyright in your contribution to
Nesqual Tech, and grants a patent licence covering it.

We are asking for this for one reason and we would rather state it plainly than
dress it up: **without copyright assignment the licence can never be changed.**
The FSL already promises that every release turns into Apache 2.0 after two
years. Honouring that promise, relicensing more permissively, or dual-licensing
for a customer who needs it all require the right to relicense the whole work.
A codebase with a hundred contributors and no assignment cannot do any of those
things — it is frozen under whatever terms it started with, and the two-year
Apache commitment becomes unkeepable for any file we did not write ourselves.

What the CLA does **not** do: it does not take your patch away from you. You
keep the right to use your own contribution however you like, including in other
projects under other licences.

A bot will comment on your first pull request with a link to sign. It takes a
minute and is remembered for every later contribution.

If you cannot sign a CLA — many employers have rules about this — please open an
issue describing the fix rather than a pull request. A well-written bug report
is genuinely valuable and carries no legal question.

## What we accept

In rough order of how likely it is to be merged quickly:

1. **Bug fixes with a failing test first.** The test that fails before your
   change and passes after it is the whole review.
2. **Risk-classification corrections.** `services/risk.py` has known false
   positives — actions classified as `send` or `spend` that are neither. These
   are the easiest high-value contributions in the repo, and we would rather
   find them by having the classifier read than by having users hit them.
   Include a test in `tests/services/`.
3. **Documentation that corrects something wrong.** Especially in
   `docs/local-dev.md` and `docs/STATUS.md`, where drift is inevitable.
4. **Connector manifests** built with `packages/connector-sdk`, with mocks, and
   with an honest `risk` declaration on every action.
5. **Platform and portability fixes** — anything that makes
   `docker compose up` work somewhere it currently does not.

## What we will refuse

Not because the work is bad, but so you do not spend an evening on something
that was never going to land:

- **Anything that lowers a risk classification** without a test proving the
  action cannot send, spend or delete. The declared risk in a connector manifest
  may raise a classification; it may never lower one. That rule is enforced in
  `services/risk.py::max_risk` and it is not up for negotiation.
- **A second execution path around `simulation.perform`.** Every outbound effect
  goes through the one chokepoint. A change that adds a side effect anywhere
  else will be refused even if it works, because the guarantee is structural and
  a second path destroys it.
- **A way to disable the approval gate globally.** Per-action configuration of
  what is risky is fine. A master switch is not.
- **Foreign keys on `audit_events` or `work_item_transfers`.** Their absence is
  the reason deleting a record cannot erase its history. It looks like a schema
  oversight; it is the design.
- **Weakening a default so a demo is easier.** If a default must change for
  local development it must remain fail-closed in production, gated on
  `NESQ_ENV` the way the development auth bypass already is.
- **Telemetry, analytics, or any outbound call the operator did not ask for.**
- **Billing, metering, licence-key checking or subscription enforcement.** Those
  belong to the managed service and are not part of this repository. A pull
  request that adds them here will be closed.
- **Large refactors arriving without warning.** Open an issue first. A 4,000-line
  diff nobody agreed to is unreviewable, and reviewing it badly is worse than
  not merging it.
- **New runtime dependencies** without a paragraph explaining why the standard
  library or an existing dependency will not do.

## Governance — who decides

Nesqual Tech maintains this repository and makes the final call on every merge.
That is not a committee and we are not pretending otherwise: it is a small team
that also runs the product in production, and the code you are changing is the
code that runs there.

What that means in practice:

- We will tell you *why* something is refused, and the reasons above are the
  ones we expect to use. If we refuse for a reason not on that list, we owe you
  the reasoning in the pull request.
- Roadmap decisions are ours. An issue asking for a feature is welcome; an
  unsolicited implementation of a large feature may be declined regardless of
  quality.
- Security and correctness beat convenience every time, including when the
  inconvenience is ours.

## How to work on it

```bash
git clone <repo> && cd nesqbot
docker compose up -d              # no configuration needed
```

See the [README](README.md) for the full quick start. For the API:

```bash
cd apps/api
python -m venv .venv && . .venv/bin/activate    # Scripts/activate on Windows
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

`npm install` requires a local disk — it does not work reliably on a network
share or a mapped drive.

### Standards

- **Python** — `ruff check .` clean, `ruff format` for formatting. Type hints on
  anything public. `mypy` config lives in `apps/api/pyproject.toml`.
- **TypeScript** — `npm run typecheck` and `npm run lint` clean. Do not repoint
  a workspace package's `exports` at `dist/`; every `package.json` explains why.
- **Tests** — the API suite must stay green. New behaviour needs a test; a bug
  fix needs the test that would have caught it.
- **Comments** — this codebase explains *why*, at length, at the point of the
  decision. Match that. A comment recording a measurement, a failure mode or a
  rejected alternative is worth more than one restating the code.
- **Commits** — imperative subject, and a body explaining the reasoning when it
  is not obvious. Small, reviewable commits over one large one.

### Never commit

A real credential, a real tenant or subscription id, a real hostname of a live
deployment, a customer name, or a `.env` file. `.gitignore` covers the obvious
cases and is not a substitute for looking at your own diff. If you commit a
secret by accident, say so immediately — rotating it is quick, and finding out
later is not.

## Support expectations

**Community support is best-effort.** Issues are read, and are answered when
someone has time; there is no response-time commitment on this repository and
you should not build a production dependency on getting one. Paid support with
an actual SLA is a separate commercial arrangement.

Security reports are the exception — see [SECURITY.md](SECURITY.md), which does
commit to a timeline.

## Reporting a bug

Include: what you ran, what you expected, what happened, `docker compose logs`
around the failure, your OS, and your Docker version. A reproduction against a
clean `docker compose up` is worth more than any amount of description.

For anything security-relevant, stop and read [SECURITY.md](SECURITY.md) first.
Do not open a public issue.
