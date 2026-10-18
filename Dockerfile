# SPEC-21 §21.2: hosted read-only demo image.
#
# No AWS Events credential is ever baked in or read at runtime; the DEMO profile
# (see src/reinvent_concierge/app_context.py) never constructs one. Everything the
# container writes goes under /tmp; intended to run with `docker run --read-only
# --tmpfs /tmp` (also set by the CDK stack's container definition).
#
# Build with `make demo-image` (never `docker build` directly): that target first produces
# infra/demo-snapshot/session_catalog.db (a synced, vectorized catalog) and the fastembed
# model cache this Dockerfile copies in below, so the running container never calls
# Hugging Face at startup (AC4).
FROM python:3.13-slim

LABEL org.opencontainers.image.description="re:Invent Autonomous Concierge — hosted read-only demo (SPEC-21)"

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin demo

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[semantic,ui]"

# Baked, read-only: the catalog + vectors snapshot and the pre-warmed fastembed model
# cache. `concierge demo serve` copies the catalog snapshot to $LOCAL_DB_PATH (/tmp) on
# first startup, since every visitor session's IngestionService calls ensure_schema()
# against it and /app is read-only at runtime.
COPY infra/demo-snapshot/session_catalog.db /opt/demo-snapshot/session_catalog.db
COPY infra/demo-snapshot/fastembed-cache /home/demo/.cache/fastembed
RUN chown -R demo:demo /home/demo/.cache

# The event the baked snapshot was synced from (infra/demo-snapshot/event_id); `make
# demo-image` and the CDK stack both pass it, so the runtime config matches the catalog.
ARG AWS_EVENTS_EVENT_ID=reinvent2026

ENV HOME=/home/demo \
    AWS_EVENTS_EVENT_ID=${AWS_EVENTS_EVENT_ID} \
    LOCAL_DB_PATH=/tmp/session_catalog.db \
    DEMO_BAKED_CATALOG_PATH=/opt/demo-snapshot/session_catalog.db \
    DEMO_SESSION_DB_DIR=/tmp/demo \
    PLANNER_DB_PATH=/tmp/planner_state.db \
    WRITE_ACCESS_ACTIVE=false

USER demo
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/_stcore/health', timeout=3)"]
ENTRYPOINT ["concierge", "demo", "serve", "--host", "0.0.0.0", "--port", "8080"]
