"""ADR-014: store UTC, display and calculate in the event zone."""

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from reinvent_concierge.util.time import (
    as_utc,
    event_zone,
    is_valid_zone,
    local_iso,
    to_event_local,
)
from tests.factories import make_session

VEGAS = ZoneInfo("America/Los_Angeles")


def test_as_utc_treats_naive_as_utc_and_converts_aware():
    assert as_utc(datetime(2026, 12, 1, 9, 0)) == datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    vegas_nine = datetime(2026, 12, 1, 9, 0, tzinfo=VEGAS)
    assert as_utc(vegas_nine) == datetime(2026, 12, 1, 17, 0, tzinfo=timezone.utc)


def test_event_zone_falls_back_for_missing_or_invalid_names():
    assert event_zone("Europe/London", "America/Los_Angeles") == ZoneInfo("Europe/London")
    assert event_zone(None, "America/Los_Angeles") == VEGAS
    assert event_zone("Not/AZone", "America/Los_Angeles") == VEGAS


def test_local_day_uses_event_zone_not_utc():
    # 01:00 UTC on Dec 2 is 17:00 on Dec 1 in Las Vegas.
    late = datetime(2026, 12, 2, 1, 0, tzinfo=timezone.utc)
    assert to_event_local(late, VEGAS).date().isoformat() == "2026-12-01"


def test_local_iso_has_offset_and_minute_precision():
    value = datetime(2026, 12, 1, 17, 0, 30, tzinfo=timezone.utc)
    assert local_iso(value, VEGAS) == "2026-12-01T09:00-08:00"


def test_localized_session_keeps_utc_fields_and_adds_local_strings():
    session = make_session("S1", start=datetime(2026, 12, 1, 17, 0, tzinfo=timezone.utc))
    localized = session.localized(VEGAS)
    assert localized.start_time == session.start_time
    assert localized.start_local == "2026-12-01T09:00-08:00"
    assert localized.end_local == "2026-12-01T10:00-08:00"


def test_zone_helpers_treat_os_errors_as_unusable_zones():
    # ZoneInfo("America") raises IsADirectoryError on Python 3.10/3.11.
    def fake_zone_info(key):
        if key == "America":
            raise IsADirectoryError(key)
        return ZoneInfo(key)

    with patch("reinvent_concierge.util.time.ZoneInfo", side_effect=fake_zone_info):
        assert is_valid_zone("America") is False
        assert is_valid_zone("Europe/London") is True
        assert event_zone("America", "Europe/London") == ZoneInfo("Europe/London")


def machine_local_zone_violations(source: str) -> list:
    """Line numbers of ``x.astimezone()`` and ``datetime.now()`` calls with no arguments."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or node.args or node.keywords:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr == "astimezone":
            found.append(node.lineno)
        elif func.attr == "now":
            owner = func.value
            dotted = (
                owner.attr
                if isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "datetime"
                else None
            )
            if (isinstance(owner, ast.Name) and owner.id == "datetime") or dotted == "datetime":
                found.append(node.lineno)
    return found


@pytest.mark.parametrize(
    "snippet, flagged",
    [
        ("x.astimezone()", True),
        ("datetime.now()", True),
        ("datetime.datetime.now()", True),
        ("x.astimezone(zone)", False),
        ("datetime.now(timezone.utc)", False),
        ("datetime.datetime.now(tz=zone)", False),
    ],
)
def test_machine_local_zone_checker_flags_violations(snippet, flagged):
    assert bool(machine_local_zone_violations(snippet)) is flagged


def test_source_never_uses_machine_local_zone():
    root = Path(__file__).resolve().parents[2] / "src" / "reinvent_concierge"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for line in machine_local_zone_violations(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(root)}:{line}")
    assert offenders == []
