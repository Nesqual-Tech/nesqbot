#!/bin/bash
# Nesq Bot Desktop supervisor.
#
# The previous version ended in `wait -n`, which returns as soon as ANY
# background job exits. A crashed x11vnc therefore took the whole container
# down silently, and nothing ever restarted a component that died on its own.
#
# This version runs a small supervisor: every component lives in a restart loop
# with exponential backoff, SIGTERM/SIGINT tear everything down in reverse
# dependency order, and "READY" is only printed once the sidecar actually
# answers /health.
#
# Environment:
#   DESKTOP_PROFILE       icewm | xfce             (default icewm)
#   DESKTOP_RESOLUTION    WIDTHxHEIGHTxDEPTH       (default 1280x800x24)
#   DESKTOP_WIDTH/HEIGHT/DEPTH                     (override individual axes)
#   VNC_PW                VNC password             (default: none -> warning)
#   NESQ_SIDECAR_PORT     sidecar port             (default 7910)
#   NESQ_SIDECAR_TOKEN    shared secret; unset = open control plane + warning
#   NESQ_STREAM_PORT      noVNC port               (default 6901)
#   BOT_SLUG              label for logs           (default bot)
#   READY_TIMEOUT_SECONDS readiness budget         (default 90)
#   NESQ_BROWSER_ENABLED  1|0 launch Chromium      (default 1)
#   NESQ_CDP_PORT         Chromium debugging port  (default 9222, loopback only)
#   NESQ_BROWSER_START_URL first tab               (default about:blank)
#   NESQ_CHROMIUM_SANDBOX auto|on|off              (default auto)
#   NESQ_CHROMIUM_EXTRA_ARGS extra chromium flags  (default empty)

set -Eeuo pipefail

# Job control so each background supervisor becomes its own process group
# leader; that is what makes `kill -- -PID` able to stop a component and every
# child it spawned (Xvfb -> WM -> Chromium) in one go.
set -m

readonly BOT_SLUG="${BOT_SLUG:-bot}"
readonly DISPLAY_NUM="${DISPLAY_NUM:-1}"
export DISPLAY=":${DISPLAY_NUM}"

readonly SIDECAR_PORT="${NESQ_SIDECAR_PORT:-7910}"
readonly STREAM_PORT="${NESQ_STREAM_PORT:-6901}"
readonly VNC_PORT="${NESQ_VNC_PORT:-5900}"
readonly READY_TIMEOUT="${READY_TIMEOUT_SECONDS:-90}"
readonly READY_FILE="${READY_FILE:-/tmp/nesq-desktop.ready}"
readonly STOP_FILE="/tmp/nesq-desktop.stopping"
readonly RUNDIR="${NESQ_SIDECAR_WORKDIR:-/tmp/nesq-sidecar}"

# Chromium's DevTools endpoint. It is unauthenticated: anything that can open a
# socket to it can read every cookie, every logged-in session and every page in
# this browser. It is therefore bound to loopback, never EXPOSEd, and reachable
# only by the sidecar - which does have a shared-secret gate. Do not "helpfully"
# publish this port.
readonly CDP_PORT="${NESQ_CDP_PORT:-9222}"
readonly CDP_ADDRESS="127.0.0.1"
readonly BROWSER_ENABLED="${NESQ_BROWSER_ENABLED:-1}"
readonly BROWSER_PROFILE="${NESQ_BROWSER_PROFILE_DIR:-$HOME/.config/chromium-nesq}"

# --- resolution -------------------------------------------------------------
IFS='x' read -r _rw _rh _rd <<< "${DESKTOP_RESOLUTION:-1280x800x24}"
SCREEN_W="${DESKTOP_WIDTH:-${_rw:-1440}}"
SCREEN_H="${DESKTOP_HEIGHT:-${_rh:-900}}"
SCREEN_D="${DESKTOP_DEPTH:-${_rd:-24}}"
case "${SCREEN_W}${SCREEN_H}${SCREEN_D}" in
    ''|*[!0-9]*)
        echo "FATAL: invalid resolution '${DESKTOP_RESOLUTION:-}' (want WIDTHxHEIGHTxDEPTH)" >&2
        exit 64
        ;;
esac
readonly SCREEN_GEOMETRY="${SCREEN_W}x${SCREEN_H}x${SCREEN_D}"
export NESQ_SCREEN_GEOMETRY="$SCREEN_GEOMETRY"

# --- logging ----------------------------------------------------------------
log()   { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${*:2}"; }
info()  { log INFO "$@"; }
warn()  { log WARN "$@" >&2; }
fatal() { log FATAL "$@" >&2; exit 1; }

stopping() { [ -e "$STOP_FILE" ]; }

# --- shutdown ---------------------------------------------------------------
declare -a SUPERVISOR_PIDS=()

shutdown_all() {
    local signal="${1:-TERM}"
    stopping && return 0
    : > "$STOP_FILE"
    info "shutdown requested (SIG${signal}) - stopping components"
    rm -f "$READY_FILE" 2>/dev/null || true

    # Reverse order: sidecar, websockify, x11vnc, WM, Xvfb.
    local idx pid
    for (( idx=${#SUPERVISOR_PIDS[@]}-1 ; idx>=0 ; idx-- )); do
        pid="${SUPERVISOR_PIDS[idx]}"
        kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done

    local waited=0
    while [ "$waited" -lt 10 ]; do
        jobs -rp | grep -q . || break
        sleep 1
        waited=$((waited + 1))
    done
    for pid in $(jobs -rp); do
        kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    done

    info "desktop stopped"
    exit 0
}
trap 'shutdown_all TERM' TERM
trap 'shutdown_all INT' INT

# --- supervision ------------------------------------------------------------
# supervise NAME MAX_BACKOFF -- cmd args...
# Restarts forever with exponential backoff. A component that exits 0 is still
# restarted - none of these are supposed to ever finish.
supervise() {
    local name="$1"; shift
    local max_backoff="$1"; shift
    [ "${1:-}" = "--" ] && shift

    (
        set +e
        trap 'exit 0' TERM INT
        local backoff=1 started rc elapsed child
        while ! stopping; do
            started=$(date +%s)
            info "starting ${name}"
            "$@" &
            child=$!
            wait "$child"
            rc=$?
            stopping && exit 0
            elapsed=$(( $(date +%s) - started ))
            warn "${name} exited rc=${rc} after ${elapsed}s - restarting in ${backoff}s"
            sleep "$backoff"
            # A component that stayed up a while gets a fresh budget; one that
            # dies instantly backs off so it cannot spin the CPU.
            if [ "$elapsed" -ge 30 ]; then
                backoff=1
            else
                backoff=$(( backoff * 2 ))
                [ "$backoff" -gt "$max_backoff" ] && backoff="$max_backoff"
            fi
        done
    ) &
    local sup=$!
    SUPERVISOR_PIDS+=("$sup")
    info "supervisor pid=${sup} component=${name}"
}

# --- readiness helpers ------------------------------------------------------
wait_for_x() {
    local deadline=$(( $(date +%s) + 30 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    return 1
}

wait_for_tcp() {
    local port="$1"
    local deadline=$(( $(date +%s) + ${2:-30} ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

wait_for_sidecar() {
    local deadline=$(( $(date +%s) + READY_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        curl -fsS --max-time 3 "http://127.0.0.1:${SIDECAR_PORT}/health" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

# --- window manager selection ----------------------------------------------
# `full` images have XFCE, `slim` images do not. Asking for xfce on a slim
# image is a config mistake, not a reason to crash-loop: warn and use IceWM.
pick_wm() {
    local want="${DESKTOP_PROFILE:-icewm}"
    case "$want" in
        xfce|xfce4)
            if command -v startxfce4 >/dev/null 2>&1; then
                echo "startxfce4"
                return
            fi
            warn "DESKTOP_PROFILE=${want} but XFCE is absent (image built with DESKTOP_PROFILE_BUILD=slim) - falling back to IceWM"
            ;;
        icewm) ;;
        *) warn "unknown DESKTOP_PROFILE='${want}' - falling back to IceWM" ;;
    esac
    if command -v icewm-session >/dev/null 2>&1; then
        echo "icewm-session"
    elif command -v icewm >/dev/null 2>&1; then
        echo "icewm"
    else
        echo ""
    fi
}

# --- chromium ---------------------------------------------------------------
chromium_bin() {
    local c
    for c in chromium chromium-browser google-chrome-stable google-chrome; do
        command -v "$c" >/dev/null 2>&1 && { echo "$c"; return; }
    done
    echo ""
}

# Chromium's own renderer sandbox needs either the setuid helper (which
# allowPrivilegeEscalation:false on AKS blocks) or an unprivileged user
# namespace (which some seccomp profiles block). Probe for the namespace and
# drop the sandbox only when the kernel actually refuses, so we keep
# defence-in-depth wherever it is available instead of hardcoding --no-sandbox
# the way most container images do.
chromium_sandbox_arg() {
    case "${NESQ_CHROMIUM_SANDBOX:-auto}" in
        on)  echo ""; return ;;
        off) echo "--no-sandbox"; return ;;
    esac
    if unshare --user true >/dev/null 2>&1; then
        echo ""
    else
        warn "unprivileged user namespaces unavailable - starting Chromium with --no-sandbox (the container/pod is the isolation boundary)"
        echo "--no-sandbox"
    fi
}

# A hard kill (OOM, SIGKILL, node eviction) leaves these behind in the
# persisted profile and the next Chromium refuses to start, which would
# crash-loop the supervisor forever on an otherwise healthy desktop.
clear_chromium_singletons() {
    rm -f "${BROWSER_PROFILE}/SingletonLock" \
          "${BROWSER_PROFILE}/SingletonSocket" \
          "${BROWSER_PROFILE}/SingletonCookie" 2>/dev/null || true
}

run_chromium() {
    local bin sandbox
    bin="$(chromium_bin)"
    sandbox="$(chromium_sandbox_arg)"
    mkdir -p "$BROWSER_PROFILE"
    clear_chromium_singletons
    local -a args=(
        "--remote-debugging-port=${CDP_PORT}"
        "--remote-debugging-address=${CDP_ADDRESS}"
        "--user-data-dir=${BROWSER_PROFILE}"
        "--window-position=0,0"
        "--window-size=${SCREEN_W},${SCREEN_H}"
        --no-first-run
        --no-default-browser-check
        --disable-gpu
        --disable-dev-shm-usage
        --disable-features=TranslateUI
        --password-store=basic
        --use-mock-keychain
        --disable-session-crashed-bubble
        --hide-crash-restore-bubble
    )
    [ -n "$sandbox" ] && args+=( "$sandbox" )
    # Deliberately NOT set: --remote-allow-origins. Widening the allowed
    # WebSocket origins would let a page the browser loads talk to its own
    # debugger. The sidecar's CDP client sends no Origin header instead.
    if [ -n "${NESQ_CHROMIUM_EXTRA_ARGS:-}" ]; then
        # shellcheck disable=SC2206
        args+=( ${NESQ_CHROMIUM_EXTRA_ARGS} )
    fi
    args+=( "${NESQ_BROWSER_START_URL:-about:blank}" )
    exec "$bin" "${args[@]}"
}

novnc_root() {
    local d
    for d in /usr/share/novnc /usr/share/webapps/novnc /opt/novnc; do
        [ -d "$d" ] && { echo "$d"; return; }
    done
    echo ""
}

# =============================================================================
main() {
    rm -f "$STOP_FILE" "$READY_FILE" 2>/dev/null || true
    mkdir -p "$RUNDIR"
    chmod 0700 "$RUNDIR"

    info "Nesq Bot Desktop booting bot=${BOT_SLUG} display=${DISPLAY} geometry=${SCREEN_GEOMETRY}"

    if [ -z "${NESQ_SIDECAR_TOKEN:-}" ]; then
        warn "############################################################"
        warn "# NESQ_SIDECAR_TOKEN is not set. The control plane on port"
        warn "# ${SIDECAR_PORT} will accept UNAUTHENTICATED mouse, keyboard"
        warn "# and screenshot requests from anything that can reach it."
        warn "# Fine on a laptop. Never in a shared cluster."
        warn "############################################################"
    fi

    # 1. X server. Nothing else can start without it.
    supervise xvfb 30 -- \
        Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -nolisten tcp -dpi 96 +extension RANDR
    wait_for_x || fatal "Xvfb did not come up on ${DISPLAY} within 30s"
    info "X display ${DISPLAY} is up"

    # 2. Window manager.
    local wm
    wm="$(pick_wm)"
    if [ -n "$wm" ]; then
        supervise "wm:${wm}" 30 -- "$wm"
    else
        warn "no window manager available - running bare X (windows will be unmanaged)"
    fi

    # 3. VNC server, bound to loopback. Only websockify is published.
    local -a vnc_args=( -display "$DISPLAY" -forever -shared -noxdamage
                        -rfbport "$VNC_PORT" -localhost -quiet )
    if [ -n "${VNC_PW:-}" ]; then
        # -passwdfile keeps the password out of `ps`, unlike -passwd.
        # Plain path (not rm:) so a supervisor restart can still read it.
        printf '%s\n' "$VNC_PW" > "${RUNDIR}/vncpasswd"
        chmod 0600 "${RUNDIR}/vncpasswd"
        vnc_args+=( -passwdfile "${RUNDIR}/vncpasswd" )
    else
        warn "VNC_PW is empty - the stream on ${STREAM_PORT} is unauthenticated"
        vnc_args+=( -nopw )
    fi
    supervise x11vnc 30 -- x11vnc "${vnc_args[@]}"
    wait_for_tcp "$VNC_PORT" 30 \
        || warn "x11vnc is not listening on ${VNC_PORT} yet - the supervisor keeps retrying"

    # 4. noVNC / websockify - the browser-facing stream.
    local web
    web="$(novnc_root)"
    if [ -n "$web" ]; then
        supervise websockify 30 -- websockify --web="$web" "${STREAM_PORT}" "localhost:${VNC_PORT}"
    else
        warn "noVNC web root not found - serving a raw websocket only"
        supervise websockify 30 -- websockify "${STREAM_PORT}" "localhost:${VNC_PORT}"
    fi

    # 5. Chromium, with the DevTools endpoint on loopback. The sidecar's
    #    /browser/* API drives this instance; the pixel API still sees it too,
    #    because it is a real window on the same X display.
    if [ "$BROWSER_ENABLED" = "1" ] && [ -n "$(chromium_bin)" ]; then
        supervise chromium 30 -- run_chromium
        if wait_for_tcp "$CDP_PORT" 40; then
            info "chromium devtools listening on ${CDP_ADDRESS}:${CDP_PORT} (loopback only)"
        else
            warn "chromium did not open ${CDP_PORT} within 40s - /browser/* will report browser_unavailable; the pixel API is unaffected"
        fi
    elif [ "$BROWSER_ENABLED" = "1" ]; then
        warn "no chromium binary in this image - /browser/* will report browser_unavailable"
    else
        info "NESQ_BROWSER_ENABLED=0 - not launching Chromium"
    fi

    # 6. Agent sidecar.
    supervise sidecar 15 -- python3 /opt/nesq/server.py

    # 7. Readiness gate. "READY" has to mean the agent can actually drive this
    #    desktop, so it waits on the sidecar instead of on a sleep.
    if wait_for_sidecar; then
        : > "$READY_FILE"
        info "READY bot=${BOT_SLUG} profile=${DESKTOP_PROFILE:-icewm} geometry=${SCREEN_GEOMETRY} stream=:${STREAM_PORT} control=:${SIDECAR_PORT} auth=$([ -n "${NESQ_SIDECAR_TOKEN:-}" ] && echo token || echo none)"
    else
        warn "sidecar did not answer /health within ${READY_TIMEOUT}s - the container stays up and supervisors keep retrying, but this desktop is NOT ready"
    fi

    # 8. Park, staying responsive to signals.
    local alive pid
    while ! stopping; do
        sleep 5 &
        wait $! 2>/dev/null || true
        stopping && break
        alive=0
        for pid in "${SUPERVISOR_PIDS[@]}"; do
            kill -0 "$pid" 2>/dev/null && alive=$((alive + 1))
        done
        if [ "$alive" -eq 0 ]; then
            fatal "every supervisor has exited - letting the orchestrator restart us"
        fi
    done
}

main "$@"
