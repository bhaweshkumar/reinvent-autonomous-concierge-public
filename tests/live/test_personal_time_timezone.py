"""Spike S3 / ADR-012: does UTC personal time round-trip and display as event-local time?

Run (writes one 30-minute personal-time block, then deletes it; never reserves):

    LIVE_ALLOW_WRITES=1 LIVE_EVENT_ID=<id> LIVE_TZ_EVENT_ID=<id> LIVE_TZ_PAUSE=1 \
        .venv/bin/python -m pytest -m live tests/live/test_personal_time_timezone.py -s

``<id>`` is a registered public Summit (for example ``summithkg26``). Open the official AWS
Events app while the test is paused and confirm the block shows at 09:00 event-local time.
The pause happens only when ``LIVE_TZ_PAUSE=1``; the block is deleted afterwards either way, and
the title prefix lets the end-of-run sweep remove it if the run is interrupted.
"""

import os
from datetime import date, datetime, time, timedelta

import pytest

from reinvent_concierge.exceptions import ReservationConflictError
from reinvent_concierge.util.time import as_utc, event_zone
from tests.live.conftest import LIVE, TEST_TITLE_PREFIX

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("require_writes")]


async def test_personal_time_timezone_roundtrip(write_client):
    tz_event_id = os.environ.get("LIVE_TZ_EVENT_ID")
    if not tz_event_id:
        pytest.skip("set LIVE_TZ_EVENT_ID (and LIVE_EVENT_ID to the same value)")
    if tz_event_id != LIVE.event_id:
        pytest.skip(f"set LIVE_EVENT_ID={tz_event_id}; the live client targets {LIVE.event_id}")

    event = await write_client.get_event()
    zone = event_zone(event.timezone, "UTC")
    if not event.start_date:
        pytest.skip("event has no start date")
    day = date.fromisoformat(event.start_date[:10])
    local_start = datetime.combine(day, time(9, 0), tzinfo=zone)
    start, end = as_utc(local_start), as_utc(local_start + timedelta(minutes=30))

    title = f"{TEST_TITLE_PREFIX} timezone probe"
    block_id = None
    try:
        try:
            block_id = await write_client.create_personal_time(
                title, start, end, "S3 timezone probe; safe to delete"
            )
        except ReservationConflictError:
            pytest.skip("CreatePersonalTime is closed right now (HTTP 409)")

        schedule = await write_client.get_schedule(include_favorites=False)
        block = next(b for b in schedule.personal_time if b.id == block_id)
        assert block.start_time == start, (block.start_time, start)
        print(f"\nCheck the official app: '{title}' must show at 09:00 {zone.key}.")
        if os.environ.get("LIVE_TZ_PAUSE") == "1":
            input("Press Enter after checking the official app...")
    finally:
        if block_id:
            await write_client.delete_personal_time(block_id)
