FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY sql ./sql
ENV PYTHONUNBUFFERED=1

# The image tag this was built as, surfaced by /api/health as `build`. Distinct
# from API_VERSION, which is the hand-maintained contract version - conflating
# the two made a successful v0.3.0 deploy look like a failed one in the app UI.
ARG NESQ_BUILD=unknown
ENV NESQ_BUILD=$NESQ_BUILD
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]