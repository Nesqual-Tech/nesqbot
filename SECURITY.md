# Security Policy

## Reporting a vulnerability

**Email `office@nesqualtech.com`.**

Please do **not** open a public issue, pull request or discussion for a security
problem. Report it privately and give us a chance to fix it before it is
described in public.

If you would like to encrypt the report, say so in a first message with no
detail in it and we will reply with a key.

### What to include

- What the issue is, and what an attacker gets out of it.
- The steps to reproduce it — ideally against a clean `docker compose up`, which
  is the configuration we can reproduce fastest.
- The version, commit or release you tested.
- Whether you have disclosed it anywhere else, and any deadline you are working
  to.

### What we commit to

| | |
| --- | --- |
| Acknowledgement of your report | within **3 working days** |
| An initial assessment — severity, and whether we can reproduce it | within **10 working days** |
| A fix or a documented mitigation for a confirmed high-severity issue | within **90 days** |
| Credit in the release notes | if you want it; tell us the name to use |

We will keep you updated as the fix progresses rather than going quiet, and we
will tell you before we publish anything that describes your report.

We do not run a paid bug bounty.

### Safe harbour

We will not pursue or support legal action against anyone who reports a
vulnerability to us in good faith, provided you:

- only test against your own installation — never against another user's data,
  and never against our hosted service without written permission first;
- do not access, modify, or retain data that is not yours;
- do not degrade the service for anyone else;
- give us reasonable time to fix the issue before disclosing it publicly.

## Supported versions

This is a young project. Security fixes land on the **latest release and `main`
only**. There is no long-term-support branch and we would rather say so than
imply one exists.

Self-hosting means you are responsible for updating. Watch the repository for
releases.

## Known gaps — read this before pointing it at real data

We publish an honest list of what is missing rather than a list of what is
present. The current one lives in [`docs/security.md`](docs/security.md) and
[`docs/STATUS.md`](docs/STATUS.md), and it is maintained, not decorative.

The items you must understand before a production deployment:

- **The development auth bypass is real.** With `NESQ_ENV=development`, a
  request with no `Authorization` header — or any request carrying
  `X-Nesq-Dev: 1` — is authenticated as a seeded development user. It is gated
  entirely on that one setting and is refused when `NESQ_ENV=production`.
  **Confirm `NESQ_ENV` is set to `production` in any deployment reachable by
  anyone but you.** This is the single most important line of configuration in
  the product.
- **There are no roles.** Every authenticated user is equal. There is ownership
  scoping — your threads and custom bots are yours, and something you cannot see
  returns 404 rather than 403 — but there is no admin/user distinction.
- **Approval scoping is incomplete.** Any authenticated user can currently
  decide an approval raised by a system bot.
- **Session tokens cannot be revoked.** HS256, 14 days, no refresh and no
  revocation list. A stolen token is valid until it expires.
- **There is no rate limiting.** The per-bot `daily_budget_usd` is a soft cost
  cap, not a request limit.
- **The risk classifier has false positives, and may have false negatives.**
  `services/risk.py` classifies most actions by name. Known false positives
  exist — actions gated as `send` or `spend` that are neither. We consider a
  false positive an annoyance and a false negative a bug: **if you find an
  action that reaches a real side effect without an approval, that is a security
  report**, not an issue.

## Reporting something that is not a vulnerability

If you find a false positive in the risk classifier — something gated that need
not be — open a normal issue or pull request. That is a bug, not a security
problem, and public discussion of it makes the classifier better faster.

## Secrets in this repository

This repository contains no live credentials, tenant identifiers, subscription
identifiers or production hostnames. Every such value is a placeholder
(`YOUR_TENANT_ID`, `your-api.example.com`, `CHANGE_ME`) or an obviously
non-routable dummy.

If you believe you have found a real secret in the tree — current or in any
release artefact — treat it as a vulnerability and email
`office@nesqualtech.com` rather than filing an issue.
