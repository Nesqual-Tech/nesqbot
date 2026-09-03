# Build context is the **repository root**, not `apps/api`, and that is the whole
# reason this file has `apps/api/` on every path.
#
# What it bought, measured against the image that was running in production:
#
#     $ docker run --rm --entrypoint sh …/nesqbot/api:v0.12.10 -c 'ls -d /bots'
#     ls: cannot access '/bots': No such file or directory
#
# The Container App sets `BOTS_DIR=/bots` and mounts only `/mnt/bot-homes`, so
# `/bots` was never there. `seed.load_bot_specs` then fell back to `DEFAULT_BOTS`
# — the deliberately minimal skeleton whose own text reads "This is a fallback
# prompt — bots/chief_of_staff.yaml was not found" — and because `seed_system`
# *reconciles* `is_system` bots on every boot rather than skipping them, each
# restart overwrote the five real prompts with those two-sentence stubs. Every
# prompt edit anybody had ever made to `bots/*.yaml` reached development (where
# compose bind-mounts `./bots:/bots:ro`) and nothing else.
#
# So the prompts are baked in and travel with the code: same image, same tag,
# same behaviour in every environment, and a rollback rolls the prompts back
# too. The dev bind mount still wins over the baked copy when it is there, which
# is what keeps "edit the YAML, `make seed`" working without a rebuild.
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY apps/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/api/app ./app
COPY apps/api/sql ./sql
# `/bots`, not `/app/bots`, because that is the path the Bicep template and
# `docker-compose.yml` already agree on — see `Settings.bots_dir`.
COPY bots /bots
ENV PYTHONUNBUFFERED=1

# The image tag this was built as, surfaced by /api/health as `build`. Distinct
# from API_VERSION, which is the hand-maintained contract version - conflating
# the two made a successful v0.3.0 deploy look like a failed one in the app UI.
ARG NESQ_BUILD=unknown
ENV NESQ_BUILD=$NESQ_BUILD
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
