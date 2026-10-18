"""SPEC-01: configuration, credentials, rate limiter and audit logging."""

import hashlib
import json

import pytest

from reinvent_concierge.api.rate_limiter import TokenBucketRateLimiter
from reinvent_concierge.app_context import create_app_context
from reinvent_concierge.audit import AuditLogger
from reinvent_concierge.config import Config
from reinvent_concierge.credentials import CredentialManager
from reinvent_concierge.exceptions import CredentialsMissingError
from reinvent_concierge.ingestion import IngestionService
from reinvent_concierge.planner import OPERATION_QUOTAS
from tests.factories import TOKEN, FakeEmbedder, audit_dump_text, read_audit


class TestConfig:
    def test_defaults_without_env_or_file(self, tmp_path):
        config = Config.from_env_or_file(tmp_path / "missing.json")

        assert config.api_rate_limit_rps == 5
        assert config.embedding_model == "all-MiniLM-L6-v2"
        assert config.write_access_active is False
        assert config.aws_events_api_base_url == "https://api.awsevents.com/v1"

    def test_loads_env_vars(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WRITE_ACCESS_ACTIVE", "true")
        monkeypatch.setenv("API_RATE_LIMIT_RPS", "3")

        config = Config.from_env_or_file(tmp_path / "missing.json")

        assert config.write_access_active is True
        assert config.api_rate_limit_rps == 3

    def test_loads_file_and_env_takes_precedence(self, monkeypatch, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "AWS_EVENTS_API_BASE_URL": "https://from-file.test/v1",
                    "embedding_model": "file-model",
                }
            )
        )
        monkeypatch.setenv("AWS_EVENTS_API_BASE_URL", "https://from-env.test/v1")

        config = Config.from_env_or_file(config_file)

        assert config.aws_events_api_base_url == "https://from-env.test/v1"
        assert config.embedding_model == "file-model"

    def test_expands_home_in_paths(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCAL_DB_PATH", "~/catalog.db")

        config = Config()

        assert "~" not in str(config.local_db_path)
        assert config.local_db_path.is_absolute()

    def test_constructing_config_has_no_filesystem_side_effects(self, tmp_path):
        Config(local_db_path=tmp_path / "nested" / "catalog.db")

        assert not (tmp_path / "nested").exists()


class TestCredentialManager:
    def test_missing_credentials_raise(self, tmp_path):
        with pytest.raises(CredentialsMissingError):
            CredentialManager(tmp_path / "credentials").get_auth_header()

    def test_auth_header_and_private_permissions(self, tmp_path):
        path = tmp_path / "credentials"
        manager = CredentialManager(path)

        manager.store_token(TOKEN)

        assert manager.get_auth_header() == {"Authorization": f"Bearer {TOKEN}"}
        assert path.stat().st_mode & 0o777 == 0o600

    def test_tightens_permissions_of_existing_file(self, tmp_path):
        path = tmp_path / "credentials"
        path.write_text("{}")
        path.chmod(0o644)

        CredentialManager(path).store_token(TOKEN)

        assert path.stat().st_mode & 0o777 == 0o600

    def test_refresh_token_round_trip(self, tmp_path):
        manager = CredentialManager(tmp_path / "credentials")
        manager.store_token(TOKEN, refresh_token="r-1")

        assert manager.get_refresh_token() == "r-1"

    def test_hash_is_full_sha256_and_empty_when_unreadable(self, tmp_path):
        path = tmp_path / "credentials"
        manager = CredentialManager(path)
        manager.store_token(TOKEN)
        assert manager.hash_token() == hashlib.sha256(TOKEN.encode()).hexdigest()

        path.write_text("not json")
        assert manager.hash_token() == ""
        assert manager.token_exists() is False

    def test_rejects_empty_token(self, tmp_path):
        with pytest.raises(ValueError):
            CredentialManager(tmp_path / "credentials").store_token("")


class TestTokenBucketRateLimiter:
    @pytest.fixture
    def clock(self, monkeypatch):
        now = [0.0]
        monkeypatch.setattr(TokenBucketRateLimiter, "_now", staticmethod(lambda: now[0]))
        return now

    async def test_fifth_request_passes_sixth_blocks_until_next_second(self, clock):
        limiter = TokenBucketRateLimiter(rate_per_second=5)

        for _ in range(5):
            assert await limiter.acquire(timeout=0)
        assert await limiter.acquire(timeout=0) is False

        clock[0] += 1.0
        assert await limiter.acquire(timeout=0)

    def test_rejects_non_positive_rate(self):
        with pytest.raises(ValueError):
            TokenBucketRateLimiter(rate_per_second=0)


def test_documented_operation_quotas_publish_limits_and_units():
    assert {
        operation: (quota.limit_per_minute, quota.unit)
        for operation, quota in OPERATION_QUOTAS.items()
    } == {
        "GetSession": (120, "request"),
        "ListSessions": (120, "request"),
        "GetSchedule": (60, "request"),
        "ReserveSessions": (30, "session ID"),
        "CancelReservation": (30, "request"),
        "AssociateFavorites": (30, "session ID"),
        "DisassociateFavorite": (30, "request"),
        "CreatePersonalTime": (30, "request"),
        "UpdatePersonalTime": (30, "request"),
        "DeletePersonalTime": (30, "request"),
    }


async def test_app_context_initializes_and_shares_operation_quota_governor(config):
    context = await create_app_context(config, embedder=FakeEmbedder())
    try:
        assert context.client.operation_quota_governor is context.operation_quota_governor
        assert context.planner_store.path == config.planner_db_path
    finally:
        await context.aclose()


async def test_app_context_applies_event_timezone_learned_from_catalog(config):
    seeded = IngestionService(config, None, embedder=None)
    await seeded.initialize()
    await seeded._catalog.set_meta("event_timezone", "Europe/London")

    context = await create_app_context(config, embedder=None)
    try:
        assert context.config.event_timezone == "Europe/London"
        assert context.agent.config.event_timezone == "Europe/London"
        assert context.client.config.event_timezone == "Europe/London"
        assert context.ingestion.config.event_timezone == "Europe/London"
    finally:
        await context.aclose()


async def test_app_context_ignores_invalid_stored_event_timezone(config):
    seeded = IngestionService(config, None, embedder=None)
    await seeded.initialize()
    await seeded._catalog.set_meta("event_timezone", "Not/AZone")

    context = await create_app_context(config, embedder=None)
    try:
        assert context.config.event_timezone == config.event_timezone
        assert context.agent.config.event_timezone == config.event_timezone
    finally:
        await context.aclose()


class TestAuditLogger:
    def test_stores_hash_never_raw_token(self, config, credentials):
        audit = AuditLogger(config.planner_db_path, credentials.hash_token)

        audit.record("RESERVE_SESSION", session_id="SEC301", status="CONFIRMED")
        audit.record("search_catalog", arguments={"query": "x"}, dry_run=True)

        raw = audit_dump_text(config.planner_db_path)
        entries = read_audit(config.planner_db_path)
        assert TOKEN not in raw
        assert entries[0]["attendee_token_hash"] == hashlib.sha256(TOKEN.encode()).hexdigest()
        assert entries[1]["action"] == "[DRY_RUN] search_catalog"

    def test_appends_without_rewriting(self, config, credentials):
        audit = AuditLogger(config.planner_db_path, credentials.hash_token)
        audit.record("A")
        first = read_audit(config.planner_db_path)

        audit.record("B")

        after = read_audit(config.planner_db_path)
        assert after[: len(first)] == first
        assert after[-1]["action"] == "B"
