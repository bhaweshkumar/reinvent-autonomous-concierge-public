"""Connectivity checks behind ``concierge doctor``: API, sign-in, registration, catalog, MCP."""

from dataclasses import dataclass
from pathlib import Path
from typing import List

import aiosqlite

from .api import AWSEventsRestClient
from .api.hosted_mcp import EXPECTED_TOOLS, HostedMCPClient
from .config import Config
from .credentials import CredentialManager
from .exceptions import AWSEventsAPIError, CredentialsMissingError, ReInventConciergeError


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


_STATUS_HINTS = {
    401: "token expired or invalid: run `concierge login`",
    403: "signed in but not registered for this event",
    404: "event not found: check AWS_EVENTS_EVENT_ID",
}


def _failure(name: str, error: Exception) -> CheckResult:
    status = getattr(error, "status_code", None)
    hint = _STATUS_HINTS.get(status, str(error)) if status else str(error)
    return CheckResult(name, False, hint)


async def check_catalog_mapping(db_path: Path) -> CheckResult:
    """Flag catalog rows synced before mapping v2 (no ``venue_source`` recorded)."""
    name = "Catalog mapping current"
    if not db_path.exists():
        return CheckResult(name, False, "no local catalog: run `concierge sync --full`")
    async with aiosqlite.connect(db_path) as db:
        try:
            # Cancelled rows never reappear in a fetched catalog, so sync cannot re-map them.
            cursor = await db.execute(
                "SELECT COUNT(*), SUM(venue_source IS NULL) FROM sessions "
                "WHERE status IS NOT 'CANCELLED'"
            )
            total, stale = await cursor.fetchone() or (0, 0)
        except aiosqlite.OperationalError:
            return CheckResult(name, False, "catalog predates mapping v2: run `concierge sync`")
        except aiosqlite.DatabaseError:
            return CheckResult(name, False, "catalog unreadable: run `concierge sync --full`")
    if not total:
        return CheckResult(name, False, "catalog is empty: run `concierge sync --full`")
    if stale:
        return CheckResult(
            name, False, f"{stale} of {total} rows predate mapping v2: run `concierge sync`"
        )
    return CheckResult(name, True, f"{total} sessions")


def check_semantic_extra() -> CheckResult:
    """Whether fastembed is installed; without it, search degrades to keyword-only (SPEC-20)."""
    name = "Semantic search"
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return CheckResult(
            name,
            False,
            "fastembed not installed; search is keyword-only. "
            "Install with: pip install '.[semantic]'",
        )
    return CheckResult(name, True, "fastembed is installed")


async def run_checks(
    config: Config, credentials: CredentialManager, include_hosted_mcp: bool = False
) -> List[CheckResult]:
    """Probe each layer in order; later checks still run when earlier ones fail."""
    results: List[CheckResult] = []
    async with AWSEventsRestClient(config, credentials) as client:
        try:
            events = await client.list_events()
            results.append(CheckResult("API reachable", True, f"ListEvents: {len(events)} events"))
        except (ReInventConciergeError, OSError) as e:
            results.append(_failure("API reachable", e))

        try:
            event = await client.get_event()
            access = "registration required" if event.authentication_required else "public"
            results.append(CheckResult("Event", True, f"{event.event_id}: {event.name} ({access})"))
        except (ReInventConciergeError, OSError) as e:
            results.append(_failure("Event", e))

        has_token = credentials.token_exists()
        results.append(
            CheckResult(
                "Token stored",
                has_token,
                str(credentials.credentials_path) if has_token else "run `concierge login`",
            )
        )

        if has_token:
            try:
                schedule = await client.get_raw_schedule()
                results.append(
                    CheckResult(
                        "Signed in and registered",
                        True,
                        f"GetSchedule: {len(schedule.get('reserved', []))} reserved, "
                        f"{len(schedule.get('favorites', []))} favorites, "
                        f"{len(schedule.get('personalTime', []))} personal time",
                    )
                )
            except (ReInventConciergeError, OSError) as e:
                results.append(_failure("Signed in and registered", e))

        try:
            sessions = client.list_sessions()
            first = await anext(sessions, None)
            await sessions.aclose()
            detail = f"first session {first.session_id}" if first else "catalog is empty"
            results.append(CheckResult("Catalog readable", True, detail))
        except (AWSEventsAPIError, CredentialsMissingError, OSError) as e:
            results.append(_failure("Catalog readable", e))

    results.append(
        CheckResult(
            "Write access",
            True,
            "enabled" if config.write_access_active else "dry-run (WRITE_ACCESS_ACTIVE=false)",
        )
    )

    if include_hosted_mcp and has_token:
        try:
            async with HostedMCPClient(
                credentials.get_token(), config.aws_events_hosted_mcp_url
            ) as mcp:
                names = set(await mcp.list_tool_names())
            missing = sorted(EXPECTED_TOOLS - names)
            results.append(
                CheckResult(
                    "Hosted MCP server",
                    not missing,
                    f"{len(names)} tools" + (f"; missing {missing}" if missing else ""),
                )
            )
        except Exception as e:  # fastmcp surfaces transport and auth failures differently
            results.append(CheckResult("Hosted MCP server", False, f"{type(e).__name__}: {e}"))
    results.append(await check_catalog_mapping(config.local_db_path))
    results.append(check_semantic_extra())
    return results
