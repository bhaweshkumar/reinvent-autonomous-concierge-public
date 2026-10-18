.PHONY: help install test test-unit test-integration test-cov test-live test-live-writes \
	lint format typecheck check login doctor sync serve serve-sse snapper run ui clean \
	demo-serve demo-snapshot demo-image demo-synth demo-deploy

VENV ?= .venv
PYTHON ?= python3
BIN := $(VENV)/bin
# AWS CLI/CDK profile for the hosted demo's infrastructure (SPEC-21); override with
# `make demo-synth AWS_PROFILE=other-profile` if needed.
AWS_PROFILE ?= bk_personal
# Event the hosted demo's catalog snapshot is synced from. Default: the registration-required
# re:Invent catalog (needs `concierge login` as a registered attendee). For testing, point it
# at a public event (`curl https://api.awsevents.com/v1/events` lists them), e.g.
# `make demo-snapshot DEMO_EVENT_ID=<public-event-id>`. The snapshot records the event it was
# synced from (infra/demo-snapshot/event_id) and the image is built for that event.
DEMO_EVENT_ID ?= reinvent2026

help:
	@echo "re:Invent Autonomous Concierge"
	@echo ""
	@echo "Setup:"
	@echo "  make install           Create $(VENV) and install the package with dev tools"
	@echo ""
	@echo "Connect:"
	@echo "  make login             Sign in with AWS Builder ID (browser)"
	@echo "  make doctor            Check API, sign-in, registration, catalog and hosted MCP"
	@echo ""
	@echo "Run (after 'make login'):"
	@echo "  make sync              Delta-sync the session catalog (concierge sync --full for a rebuild)"
	@echo "  make serve             MCP server over stdio (Claude Desktop)"
	@echo "  make serve-sse         MCP server over SSE on 127.0.0.1:8000 (needed by the UI)"
	@echo "  make ui                Streamlit UI on http://127.0.0.1:8501"
	@echo "  make snapper           Seat snapper worker"
	@echo "  make run               Catalog sync and seat snapper runtime coordinator"
	@echo ""
	@echo "Hosted demo (SPEC-21, no AWS Events writes possible):"
	@echo "  make demo-serve        Run the demo locally (profile=DEMO, simulated schedule)"
	@echo "  make demo-snapshot     Sync a read-only catalog snapshot into infra/demo-snapshot/"
	@echo "                         (DEMO_EVENT_ID=$(DEMO_EVENT_ID); set a public event ID for tests)"
	@echo "  make demo-image        Build the demo container image (needs demo-snapshot first)"
	@echo "  make demo-synth        cdk synth: renders the CloudFormation template, deploys nothing"
	@echo "  make demo-deploy       cdk deploy: the actual deploy step"
	@echo "                         (demo-synth/demo-deploy use AWS_PROFILE=$(AWS_PROFILE))"
	@echo ""
	@echo "Quality:"
	@echo "  make test              All tests (unit + integration)"
	@echo "  make test-unit         Unit tests only"
	@echo "  make test-integration  Integration tests only"
	@echo "  make test-cov          Tests with coverage report"
	@echo "  make test-live         Live tests against the real API (read-only)"
	@echo "  make test-live-writes  Live tests incl. self-cleaning schedule writes"
	@echo "  make lint              ruff lint + format check"
	@echo "  make typecheck         mypy"
	@echo "  make check             lint + typecheck + test"
	@echo "  make format            Auto-format and fix imports"
	@echo "  make clean             Remove caches and build artifacts"

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

install: $(BIN)/python
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev,semantic,ui]"

test:
	$(BIN)/pytest

test-unit:
	$(BIN)/pytest tests/unit

test-integration:
	$(BIN)/pytest tests/integration

test-cov:
	$(BIN)/pytest --cov --cov-report=term-missing --cov-report=html

test-live:
	$(BIN)/pytest tests/live -m live -rs

test-live-writes:
	LIVE_ALLOW_WRITES=1 $(BIN)/pytest tests/live -m live -rs

login:
	$(BIN)/concierge login

doctor:
	$(BIN)/concierge doctor --mcp

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

typecheck:
	$(BIN)/mypy

check: lint typecheck test

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

sync:
	$(BIN)/concierge sync

serve:
	$(BIN)/concierge serve --transport stdio

serve-sse:
	$(BIN)/concierge serve --transport sse

ui:
	$(BIN)/concierge ui

snapper:
	$(BIN)/concierge snapper

run:
	$(BIN)/concierge run

clean:
	find . -type d -name "__pycache__" -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist src/*.egg-info

# ---------------------------------------------------------------------- hosted demo (SPEC-21)

demo-serve:
	$(BIN)/concierge demo serve --host 127.0.0.1 --port 8080

demo-snapshot:
	mkdir -p infra/demo-snapshot
	rm -f infra/demo-snapshot/session_catalog.db infra/demo-snapshot/event_id
	AWS_EVENTS_EVENT_ID=$(DEMO_EVENT_ID) LOCAL_DB_PATH=$(CURDIR)/infra/demo-snapshot/session_catalog.db \
		$(BIN)/concierge sync --full
	echo "$(DEMO_EVENT_ID)" > infra/demo-snapshot/event_id
	$(BIN)/python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"
	cp -r $${HOME}/.cache/fastembed infra/demo-snapshot/fastembed-cache

demo-image: demo-snapshot
	docker build --build-arg AWS_EVENTS_EVENT_ID=$$(cat infra/demo-snapshot/event_id) \
		-t reinvent-concierge-demo:latest .

infra/demo-snapshot/session_catalog.db:
	$(error infra/demo-snapshot/ is missing; run "make demo-snapshot" first (the default \
	DEMO_EVENT_ID needs a registered sign-in; a public event ID does not) -- cdk synth/deploy \
	build the Docker image themselves and need it too)

# The `cdk` CLI itself is Node/npm-distributed (`npm install -g aws-cdk`), never pip; this
# only prepares the Python side CDK code imports (aws-cdk-lib, constructs, cdk-ecr-deployment).
# infra/cdk.json's "app" command points straight at ".venv/bin/python3", so no PATH tricks
# are needed for `cdk` to pick up this venv (a PATH-prepend approach was tried first and
# didn't reliably resolve the venv's interpreter; a direct path always will).
infra/.venv/bin/python:
	cd infra && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

demo-synth: infra/.venv/bin/python infra/demo-snapshot/session_catalog.db
	cd infra && AWS_PROFILE=$(AWS_PROFILE) cdk synth

demo-deploy: infra/.venv/bin/python infra/demo-snapshot/session_catalog.db
	cd infra && AWS_PROFILE=$(AWS_PROFILE) cdk deploy
