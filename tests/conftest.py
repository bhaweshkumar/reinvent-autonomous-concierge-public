"""Shared pytest fixtures."""

import pytest

from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.config import Config
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.waitlist import WaitlistStore
from tests.factories import API_BASE, EVENT_ID, TOKEN, TOKEN_URL

_ENV_VARS = (
    "AWS_EVENTS_API_BASE_URL",
    "AWS_EVENTS_EVENT_ID",
    "WRITE_ACCESS_ACTIVE",
    "API_RATE_LIMIT_RPS",
    "EMBEDDING_MODEL",
    "LOCAL_DB_PATH",
    "PLANNER_DB_PATH",
    "AUDIT_LOG_PATH",
    "WAITLIST_PATH",
    "AWS_EVENTS_CREDENTIALS_PATH",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Keep developer env vars and any local .env file out of tests."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        aws_events_api_base_url=API_BASE,
        aws_events_event_id=EVENT_ID,
        aws_events_oauth_token_url=TOKEN_URL,
        local_db_path=tmp_path / "catalog.db",
        planner_db_path=tmp_path / "planner_state.db",
        audit_log_path=tmp_path / "audit_log.json",
        waitlist_path=tmp_path / "waitlist.json",
        aws_events_credentials_path=tmp_path / "credentials",
        write_access_active=False,
    )


@pytest.fixture
def write_config(config) -> Config:
    return config.model_copy(update={"write_access_active": True})


@pytest.fixture
def credentials(config) -> CredentialManager:
    manager = CredentialManager(config.aws_events_credentials_path)
    manager.store_token(TOKEN)
    return manager


@pytest.fixture
def audit(config, credentials) -> AuditLogger:
    return AuditLogger(config.planner_db_path, credentials.hash_token)


@pytest.fixture
def waitlist(config) -> WaitlistStore:
    return WaitlistStore(config.planner_db_path)
