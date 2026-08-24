FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WORKER_HEALTH_FILE=/app/run/worker.health

WORKDIR /app

RUN groupadd --system --gid 1001 nesq \
 && useradd --system --uid 1001 --gid nesq --home-dir /app --shell /usr/sbin/nologin nesq

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker ./worker

RUN mkdir -p /app/run && chown -R nesq:nesq /app

USER nesq

# The worker refreshes $WORKER_HEALTH_FILE every 15s once it is serving the
# task queue; the probe fails when that marker goes stale.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "worker.main", "--healthcheck"]

CMD ["python", "-m", "worker.main"]
