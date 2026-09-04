# Nesq Bot 0.12.0 — attachments, search, roles

Desktop 0.12.0 · 2026-09-04

## What you can do now

**Attach files to a message.** The paperclip in the composer, a paste, or a
drop onto the box. Images (PNG, JPEG, WebP, GIF, up to 4 MB) are shown to the
bot as pictures — "what does this chart say?" works without a desktop or a
connector. Text files (txt, md, csv, tsv, json, log, up to 256 KB) are read
into the prompt under the file's name. Up to four per message. Images appear as
thumbnails in the transcript and open full-size on click; other files save on
click. PDFs and Office documents are not parsed — paste the text.

**Find a conversation by what was said in it.** The sidebar search now also
asks the API for messages that contain the words, across every conversation you
own, and lists them under the title matches with a snippet and who said it.

**Rename and pin.** Both in the conversation menu (the `…` in the header).
Pinned conversations sit at the top of the list. Neither moves the conversation
in the "last spoken in" order — renaming something from a month ago does not
make it look like somebody just replied.

**Drafts survive switching.** Text you have not sent yet is kept per
conversation. Arrow-up in an empty box brings back the last thing you sent.

**Handovers are drawn again.** The rail between two messages that shows one bot
handing work to another had been dark since the API stopped sending the field
it reads. It is back.

## For administrators

**Roles.** Every account is `admin` or `member`. Admins may edit the shared
system bots and their budgets, register or remove connectors in the shared
catalog, reseed the bots, and change other people's roles. Nothing changes for
a deployment with no admin — enforcement starts the moment one exists. Grant the
first one with `ADMIN_EMAILS` on the API; promote others from `PATCH
/users/{id}` (a desktop page for this is next). Admins carry a badge in the
account box.

**Session refresh.** `POST /auth/refresh` trades a session token for a new
14-day one and revokes the old one in the same step.

**Rate limiting.** `RATE_LIMIT_PER_MINUTE` (and `RATE_LIMIT_BURST`) on the API.
Off by default.

## Fixes

- The desktop, the mobile app and anything else reading a transcript now
  receive `message.meta` — handover notes, ledger keys, and attachment lists.
- Docker Compose: the Temporal health check aimed at loopback, which the server
  never listens on, so the worker container never started. It checks the
  address the server actually binds now.

## Upgrading

The API adds two columns and two indexes on boot (`users.role`,
`threads.pinned`, a trigram index on `messages.content`), all `IF NOT EXISTS`.
It also enables the `pg_trgm` extension; the Bicep template now allow-lists it
at the server level (`azure.extensions`), so redeploy the template before the
API on Azure — without it the index statement fails and search falls back to a
sequential scan, which still works. No client change is required for the old
message shape — `attachments` is optional everywhere.
