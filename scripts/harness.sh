#!/usr/bin/env bash
# Stand up a throwaway Nesq Bot you can click: API + Postgres + the desktop
# app's dev server, all disposable, none of it touching a real deployment.
#
# Why this exists, and it is worth being blunt about it. Four user-visible bugs
# shipped in one day — a chat pane whose layout broke, a wizard rendering
# "signal is aborted without reason", turns that died in silence, and a
# reasoning model at 100% of its quota — and every one of them survived a green
# test suite and a clean `tsc`. They were all found in the end by opening the
# app in a browser and clicking Send. Nothing about `pytest` or `tsc` was going
# to catch "the reply never arrives", because both were passing while it did
# not arrive.
#
# The thing that made that expensive was the *setup* cost, so this removes it:
#
#   scripts/harness.sh up        # bring it all up, print the URL
#   scripts/harness.sh down      # delete everything it made
#   scripts/harness.sh logs      # follow the API's log
#
# Two environment facts this works around, both specific to developing on a
# Windows host with the repo on a UNC share (\\NAS\...):
#
# * npm and esbuild cannot resolve workspace links over a UNC path, and
#   `npx` refuses a UNC working directory outright ("UNC paths are not
#   supported"), so the dev server is run from a local-drive export rather than
#   from the share.
# * Docker cannot bind-mount from a UNC path either, which is why the schema is
#   piped into psql instead of mounted as an init script.
#
# The API runs with NESQ_ENV=development, which is what enables the
# `X-Nesq-Dev: 1` bypass the app's dev build uses instead of Entra — see
# `apps/desktop/src/api/client.ts::DEV_AUTH_BYPASS`. It is also why the vite
# *dev server* is used and not a production bundle: that flag is compiled out
# of a real build, so a packaged app cannot talk to this.
#
# With no model credentials the router answers with its deterministic mock,
# which is enough to exercise every path that is not the model itself: send,
# stream, tool calls, work items, approvals, the transcript surviving a tab
# switch. Point AZURE_OPENAI_ENDPOINT/AZURE_OPENAI_API_KEY at something real in
# your shell before `up` and it will use it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NET=nesq-harness
PG=nesq-h-pg
API=nesq-h-api
API_PORT="${NESQ_HARNESS_API_PORT:-18080}"
WEB_PORT="${NESQ_HARNESS_WEB_PORT:-1420}"
# A local drive, because the dev server cannot run from the share. Overridable
# for anyone whose C: is precious.
WORKTREE="${NESQ_HARNESS_DIR:-/c/nesqharness}"
IMAGE="${NESQ_HARNESS_IMAGE:-nesq-harness-api:local}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

up() {
  say "building the api image from this working tree"
  docker build -q --build-arg NESQ_BUILD=harness -f "$REPO_ROOT/apps/api/Dockerfile" -t "$IMAGE" "$REPO_ROOT" >/dev/null

  say "postgres"
  docker network create "$NET" >/dev/null 2>&1 || true
  docker rm -f "$PG" >/dev/null 2>&1 || true
  docker run -d --rm --name "$PG" --network "$NET" \
    -e POSTGRES_USER=nesq -e POSTGRES_PASSWORD=nesq -e POSTGRES_DB=nesqbot \
    pgvector/pgvector:pg16 >/dev/null
  # Wait for readiness rather than sleeping a guess: a cold pgvector image on a
  # slow disk takes noticeably longer than a warm one.
  for _ in $(seq 1 30); do
    docker exec "$PG" pg_isready -U nesq -d nesqbot >/dev/null 2>&1 && break
    sleep 1
  done
  # Lexical order matters: extensions before the tables that declare
  # vector(1536) columns, same as the compose init directory.
  docker exec -i "$PG" psql -U nesq -d nesqbot -q < "$REPO_ROOT/infra/postgres/00-extensions.sql"
  docker exec -i "$PG" psql -U nesq -d nesqbot -q < "$REPO_ROOT/apps/api/sql/init.sql" >/dev/null 2>&1 || true

  say "api on http://localhost:$API_PORT/api"
  docker rm -f "$API" >/dev/null 2>&1 || true
  # MSYS_NO_PATHCONV stops git-bash rewriting `/bots` into a Windows path
  # before docker sees it — which silently made the API fall back to its
  # built-in skeleton prompts the first time this was set up by hand.
  MSYS_NO_PATHCONV=1 docker run -d --rm --name "$API" --network "$NET" -p "$API_PORT:8080" \
    -e NESQ_ENV=development \
    -e DATABASE_URL=postgresql+asyncpg://nesq:nesq@$PG:5432/nesqbot \
    -e BOTS_DIR=/bots \
    -e NESQ_SESSION_SECRET=harness-only-not-a-real-secret \
    -e AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}" \
    -e AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}" \
    "$IMAGE" >/dev/null
  for _ in $(seq 1 40); do
    curl -sf "http://localhost:$API_PORT/api/health" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -s "http://localhost:$API_PORT/api/health" || true

  say "exporting the working tree to $WORKTREE (the dev server cannot run from the share)"
  rm -rf "$WORKTREE"
  mkdir -p "$WORKTREE"
  # HEAD, not the dirty tree: a harness that silently tests uncommitted-and-
  # then-reverted code is worse than no harness. Commit, then click.
  git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORKTREE"
  # node_modules is reused rather than installed: npm cannot complete over the
  # share, and a local install here would take minutes per run.
  for src in "$REPO_ROOT/node_modules" "$REPO_ROOT/apps/desktop/node_modules"; do
    dest="$WORKTREE${src#$REPO_ROOT}"
    if [ -d "$src" ] && [ ! -d "$dest" ]; then cp -r "$src" "$dest"; fi
  done
  printf 'VITE_API_URL=http://localhost:%s/api\n' "$API_PORT" > "$WORKTREE/apps/desktop/.env.local"

  say "dev server"
  (cd "$WORKTREE/apps/desktop" && node ../../node_modules/vite/bin/vite.js --port "$WEB_PORT" --strictPort > /tmp/nesq-harness-vite.log 2>&1 &)
  sleep 6
  cat <<EOF

  Open  http://localhost:$WEB_PORT/

  The setup wizard will ask for the backend: http://localhost:$API_PORT/api
  Auth is the dev bypass, so there is no sign-in step.
  With no model credentials every bot answers with the deterministic mock.

  scripts/harness.sh logs   follow the API log
  scripts/harness.sh down   delete all of it
EOF
}

down() {
  say "removing the harness"
  docker rm -f "$API" "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  # The dev server is a bare `node`, started detached above; kill it by port.
  if command -v powershell >/dev/null 2>&1; then
    powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort $WEB_PORT -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORKTREE"
  echo "gone"
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  logs) docker logs -f "$API" ;;
  *) echo "usage: scripts/harness.sh [up|down|logs]" >&2; exit 2 ;;
esac
