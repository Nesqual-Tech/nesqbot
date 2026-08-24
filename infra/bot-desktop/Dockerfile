# Nesq Bot Desktop - a disposable Linux desktop one bot drives.
#
# Build:
#   docker build -t nesqbot/bot-desktop:local infra/bot-desktop
#   docker build --build-arg DESKTOP_PROFILE_BUILD=full -t nesqbot/bot-desktop:xfce infra/bot-desktop
#
# Runtime contract:
#   6901/tcp  noVNC stream (browser-embeddable; Tauri + Expo WebView use it)
#   7910/tcp  agent sidecar - /health /screenshot /windows /action /clipboard_get
#             plus /browser/* (Chrome DevTools Protocol control)
#   9222/tcp  Chromium remote debugging - LOOPBACK ONLY, deliberately NOT
#             EXPOSEd. It is unauthenticated and equivalent to full control of
#             the browser and of every session logged in inside it; the sidecar
#             is its only client. Never publish it, never bind it to 0.0.0.0.
#   /home/nesq is the persisted bot home (bind mount or PVC)

# Pinned via a build arg so CI can swap in a digest without editing the file:
#   --build-arg DEBIAN_TAG=bookworm-slim@sha256:...
ARG DEBIAN_TAG=bookworm-slim
FROM debian:${DEBIAN_TAG}

# slim = IceWM only (default). full = also install XFCE.
# The original image installed both and only ever ran one; XFCE alone is
# ~350 MB of the layer.
ARG DESKTOP_PROFILE_BUILD=slim

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/nesq \
    DISPLAY=:1 \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VENV=/opt/nesq/venv \
    NESQ_SIDECAR_PORT=7910 \
    NESQ_SIDECAR_WORKDIR=/tmp/nesq-sidecar \
    NESQ_CDP_HOST=127.0.0.1 \
    NESQ_CDP_PORT=9222

# One apt layer only. Two `apt-get update`s meant two index downloads and a
# cache that could disagree with itself between them.
#
# On version pinning: Debian point releases move package versions constantly,
# so `pkg=exact.version` here rots within weeks and breaks every rebuild. The
# reproducibility knob that actually works is DEBIAN_TAG above (pin a digest);
# apt then resolves a fixed snapshot of that release.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        curl \
        dbus-x11 \
        fonts-dejavu-core \
        icewm \
        procps \
        python3 \
        python3-venv \
        scrot \
        tini \
        wmctrl \
        x11-utils \
        x11vnc \
        xclip \
        xdotool \
        xterm \
        xvfb \
        novnc \
        websockify \
    ; \
    if [ "$DESKTOP_PROFILE_BUILD" = "full" ]; then \
        apt-get install -y --no-install-recommends xfce4 xfce4-terminal; \
    fi; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*

# Record what was baked in so the sidecar's /health can report it honestly.
RUN printf '%s\n' "$DESKTOP_PROFILE_BUILD" > /opt/nesq-build-profile

RUN useradd -m -u 1000 -s /bin/bash nesq \
    && mkdir -p /home/nesq /opt/nesq \
    && chown -R nesq:nesq /home/nesq /opt/nesq

# A venv instead of `pip install --break-system-packages`: PEP 668 marks the
# system interpreter as externally managed for a reason, and overriding it lets
# pip fight apt over shared dependencies.
COPY sidecar/requirements.txt /opt/nesq/sidecar-requirements.txt
RUN set -eux; \
    python3 -m venv "$VENV"; \
    "$VENV/bin/pip" install --no-cache-dir --upgrade pip==25.0.1 setuptools==78.1.0 wheel==0.45.1; \
    "$VENV/bin/pip" install --no-cache-dir -r /opt/nesq/sidecar-requirements.txt; \
    find "$VENV" -name '__pycache__' -type d -prune -exec rm -rf {} +
ENV PATH="/opt/nesq/venv/bin:${PATH}"

COPY sidecar/server.py /opt/nesq/server.py
COPY sidecar/browser.py /opt/nesq/browser.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh \
    && chown -R nesq:nesq /opt/nesq

USER nesq
WORKDIR /home/nesq

# Transient state (screenshots, the VNC password file) goes to a 0700 directory
# under /tmp that the entrypoint creates and validates at boot - never to the
# persisted home, and never to a predictable world-readable path like the
# fixed /tmp/nesq-shot.png the original sidecar used, which any other user in
# the container could read or pre-create as a symlink. On AKS /tmp is an
# in-memory emptyDir (see k8s/desktop-template.yaml), so none of it survives.
RUN mkdir -p /home/nesq/.cache && chmod 0700 /home/nesq/.cache

# 9222 is intentionally absent: publishing the debugging port would hand
# anyone who can reach it the browser cookie jar.
EXPOSE 6901 7910

# The entrypoint gates readiness on the sidecar answering /health, so this
# probe going green means the whole desktop is usable, not just "a process
# started". start-period covers Xvfb + WM + VNC + uvicorn boot.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=4 \
    CMD curl -fsS "http://127.0.0.1:${NESQ_SIDECAR_PORT}/health" >/dev/null || exit 1

# tini reaps the zombies Chromium and the WM leave behind and forwards SIGTERM
# to the entrypoint so its trap can shut components down cleanly.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/entrypoint.sh"]
