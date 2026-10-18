"""Tier 0 - public catalog: no sign-in needed. Verifies transport, pagination and mapping."""

import time
from zoneinfo import ZoneInfo

import pytest

from reinvent_concierge.api.mapping import parse_level
from reinvent_concierge.exceptions import AuthenticationRequiredError, AWSEventsAPIError
from reinvent_concierge.ingestion import IngestionService
from reinvent_concierge.models import VALID_LEVELS
from tests.live.conftest import LIVE, build_config, raw_walk

pytestmark = pytest.mark.live


async def test_list_events_returns_the_configured_event(anonymous_client):
    events = {e.event_id: e for e in await anonymous_client.list_events()}

    event = events.get(LIVE.event_id)
    assert event is not None, f"{LIVE.event_id} missing from ListEvents: {sorted(events)}"
    assert event.name and event.start_date and event.end_date
    assert event.timezone, "event timezone is needed to interpret session times"


async def test_include_past_is_a_superset(anonymous_client):
    current = {e.event_id for e in await anonymous_client.list_events()}
    with_past = {e.event_id for e in await anonymous_client.list_events(include_past=True)}

    assert current <= with_past
    assert len(with_past) > len(current)


async def test_get_event_matches_list_entry(anonymous_client):
    listed = {e.event_id: e for e in await anonymous_client.list_events()}[LIVE.event_id]

    assert await anonymous_client.get_event(LIVE.event_id) == listed


async def test_unknown_event_is_404(anonymous_client):
    with pytest.raises(AWSEventsAPIError) as error:
        await anonymous_client.get_event("concierge-no-such-event")

    assert error.value.status_code == 404


async def test_registration_only_catalog_requires_sign_in(anonymous_client):
    event = await anonymous_client.get_event(LIVE.event_id)
    if not event.authentication_required:
        pytest.skip(f"{LIVE.event_id} has a public catalog")

    with pytest.raises(AuthenticationRequiredError):
        async for _ in anonymous_client.list_sessions(LIVE.event_id):
            pass


async def test_public_catalog_walk_is_complete(anonymous_client, anonymous_config, public_event):
    raw, total = await raw_walk(anonymous_config, public_event.event_id)

    parsed = [s async for s in anonymous_client.list_sessions(public_event.event_id)]

    assert len(raw) == total, "raw pagination lost sessions"
    schedulable = [i for i in raw if (i.get("sessionTime") or {}).get("date")]
    assert len(parsed) == len(schedulable), "client skipped or duplicated sessions"
    assert len({s.session_id for s in parsed}) == len(parsed)


async def test_public_sessions_map_faithfully(anonymous_client, anonymous_config, public_event):
    raw, _ = await raw_walk(anonymous_config, public_event.event_id)
    parsed = {s.session_id: s async for s in anonymous_client.list_sessions(public_event.event_id)}

    for item in raw:
        session = parsed.get(item["sessionId"])
        if session is None:
            continue
        wire_time = item["sessionTime"]
        zone = ZoneInfo(wire_time.get("timezone") or public_event.timezone)
        local = session.start_time.astimezone(zone)
        assert session.start_time.utcoffset().total_seconds() == 0
        assert local.strftime("%Y-%m-%d") == wire_time["date"]
        assert local.strftime("%H:%M") == wire_time["time"]
        minutes = (session.end_time - session.start_time).total_seconds() / 60
        assert minutes == int(wire_time.get("length") or 0)
        assert session.title == item["title"]
        assert session.level == parse_level(item.get("level"))
        assert session.level in (None, *VALID_LEVELS)


async def test_get_session_matches_list_entry(anonymous_client, public_event):
    listed = []
    async for session in anonymous_client.list_sessions(public_event.event_id):
        listed.append(session)
        if len(listed) == 3:
            break

    for session in listed:
        fetched = await anonymous_client.get_session(session.session_id, public_event.event_id)
        assert fetched == session


async def test_unknown_session_is_404(anonymous_client, public_event):
    with pytest.raises(AWSEventsAPIError) as error:
        await anonymous_client.get_session("NO-SUCH-SESSION-0", public_event.event_id)

    assert error.value.status_code == 404


async def test_public_catalog_sync_is_idempotent_and_searchable(
    tmp_path, anonymous_client, public_event, embedder
):
    config = build_config(tmp_path, public_event.event_id, tmp_path / "no-credentials")
    ingestion = IngestionService(config, anonymous_client, embedder=embedder)

    synced = await ingestion.initial_sync()
    changed = await ingestion.incremental_sync()
    await ingestion.search("warm up the embedder")
    started = time.perf_counter()
    results = await ingestion.search("generative AI agents", limit=10)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert synced > 0
    assert changed == 0, "an unchanged live catalog must produce an empty delta"
    assert results and all(r.status == "OPEN" for r in results)
    if LIVE.real_embeddings:
        assert elapsed_ms < 150, f"search took {elapsed_ms:.0f} ms"
