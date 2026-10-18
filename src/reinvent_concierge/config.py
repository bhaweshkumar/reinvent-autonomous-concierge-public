"""Configuration management for re:Invent Autonomous Concierge."""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_HOME = Path.home() / ".aws-events"
DEFAULT_CONFIG_FILE = APP_HOME / "config.json"


class Config(BaseSettings):
    """Application settings; each field maps to the upper-cased env var of the same name."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    aws_events_api_base_url: str = "https://api.awsevents.com/v1"
    aws_events_event_id: str = "reinvent2026"
    aws_events_mcp_server_url: str = Field(
        default="http://127.0.0.1:8000/sse",
        description="SSE endpoint of the local FastMCP server (used by the UI)",
    )
    aws_events_hosted_mcp_url: str = "https://api.awsevents.com/mcp"
    aws_events_oauth_token_url: str = "https://oauth.awsevents.com/oauth2/token"  # noqa: S105
    aws_events_oauth_authorize_url: str = "https://oauth.awsevents.com/oauth2/authorize"
    aws_events_oauth_revoke_url: str = "https://oauth.awsevents.com/oauth2/revoke"
    aws_events_oauth_client_id: str = "7vmom55m1qstvq8i71ph127bfq"
    event_timezone: str = Field(
        default="America/Los_Angeles",
        description="IANA zone for session times that carry no timezone of their own",
    )

    local_db_path: Path = APP_HOME / "session_catalog.db"
    planner_db_path: Path = APP_HOME / "planner_state.db"
    # Waitlist and audit now live in planner_db_path (SPEC-18 §18.3); these two paths name
    # only the legacy JSON files a one-time startup import reads and then renames to
    # ``.migrated``, so they stay valid after the files are gone.
    audit_log_path: Path = APP_HOME / "audit_log.json"
    waitlist_path: Path = APP_HOME / "waitlist.json"
    aws_events_credentials_path: Path = APP_HOME / "credentials"

    write_access_active: bool = Field(
        default=False, description="False until reservations open (Oct 8): dry-run mode"
    )
    api_rate_limit_rps: int = Field(default=5, ge=1)
    embedding_model: str = "all-MiniLM-L6-v2"
    snapper_poll_interval_seconds: int = Field(default=60, ge=1)
    planner_max_candidates: int = Field(default=250, ge=1, le=250)
    planner_candidate_scan_limit: int = Field(default=500, ge=1, le=1000)
    planner_solve_timeout_seconds: int = Field(default=8, ge=1, le=60)
    planner_beam_width: int = Field(default=64, ge=1, le=256)
    reserved_seat_lead_minutes: int = Field(
        default=10,
        ge=0,
        description="Minutes before a reservable session's start by which re:Invent is assumed "
        "to release unclaimed reserved seats (SPEC-19 §19.1); confirm before the event",
    )
    quota_default_timeout_seconds: float = Field(
        default=60.0,
        ge=0,
        description="How long an AWS Events API call waits for quota headroom before giving "
        "up (SPEC-20 §20.3); was a hardcoded Protocol default, now configurable",
    )

    # SPEC-21: hosted read-only demo. Only read when `profile="DEMO"` is passed to
    # create_app_context; LIVE mode never touches these.
    demo_session_db_dir: Path = Field(
        default=Path("/tmp/demo"),  # noqa: S108 - intentional: per-visitor demo state, not secret
        description="Directory holding each demo visitor session's own planner SQLite DB",
    )
    demo_max_concurrent_sessions: int = Field(default=50, ge=1)
    demo_session_idle_minutes: int = Field(default=30, ge=1)
    demo_plan_rate_limit_per_minute: int = Field(default=10, ge=1)
    demo_baked_catalog_path: Optional[Path] = Field(
        default=None,
        description="Read-only catalog+vectors snapshot baked into the demo image; copied to "
        "local_db_path on first startup since every visitor session re-runs ensure_schema()",
    )

    @field_validator(
        "local_db_path",
        "planner_db_path",
        "audit_log_path",
        "waitlist_path",
        "aws_events_credentials_path",
        "demo_session_db_dir",
    )
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("demo_baked_catalog_path")
    @classmethod
    def _expand_user_optional(cls, value: Optional[Path]) -> Optional[Path]:
        return value.expanduser() if value is not None else None

    @classmethod
    def from_env_or_file(cls, config_path: Optional[Path] = None) -> "Config":
        """Load settings with precedence: env vars > config file > defaults."""
        path = (config_path or DEFAULT_CONFIG_FILE).expanduser()
        file_values: Dict[str, Any] = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            # Init kwargs beat env vars in pydantic-settings, so drop keys the env already sets.
            file_values = {
                key.lower(): value for key, value in raw.items() if key.upper() not in os.environ
            }
        return cls(**file_values)
