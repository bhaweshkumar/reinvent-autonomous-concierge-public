"""SPEC-03: catalog sync (full, delta, single) and hybrid RRF search."""

import sqlite3
import time

import pytest

from reinvent_concierge.exceptions import AWSEventsAPIError, SyncError
from reinvent_concierge.ingestion import IngestionService, reciprocal_rank_fusion
from reinvent_concierge.ingestion.catalog_store import content_hash
from reinvent_concierge.models import Event
from tests.factories import FakeEmbedder, at, event, make_session


class FakeCatalogClient:
    def __init__(self, sessions, events=None):
        self.sessions = {s.session_id: s for s in sessions}
        self.events = events if events is not None else [event()]
        self.fail_listing = False

    async def get_event(self, event_id=None):
        for known in self.events:
            if known.event_id == event_id:
                return known
        raise AWSEventsAPIError(404, "event not found")

    async def list_sessions(self, event_id=None):
        if self.fail_listing:
            raise AWSEventsAPIError(503, "down")
        for session in self.sessions.values():
            yield session

    async def get_session(self, session_id):
        if session_id not in self.sessions:
            raise AWSEventsAPIError(404, "not found")
        return self.sessions[session_id]


def catalog():
    return [
        make_session(
            "AI301",
            title="Building agents with Amazon Bedrock",
            description="Generative AI apps",
            is_hero_speaker=True,
        ),
        make_session("AI201", title="Intro to Bedrock", level=200, venue="Wynn"),
        make_session("SEC301", title="Zero trust security", venue="Caesars Forum"),
        make_session("FULL301", title="Bedrock at scale", status="FULL"),
        make_session("SVL301", title="Serverless patterns", room_name="Level 2"),
    ]


@pytest.fixture
def fake_client():
    return FakeCatalogClient(catalog())


@pytest.fixture
async def service(config, fake_client):
    ingestion = IngestionService(config, fake_client, embedder=FakeEmbedder())
    await ingestion.initial_sync()
    return ingestion


def db_rows(config, sql, params=()):
    with sqlite3.connect(config.local_db_path) as db:
        return db.execute(sql, params).fetchall()


def vector_row_count(config, model_id=FakeEmbedder.model_id):
    return db_rows(config, "SELECT COUNT(*) FROM session_vectors WHERE model_id = ?", (model_id,))[
        0
    ][0]


class TestReciprocalRankFusion:
    def test_documents_in_both_rankings_win(self):
        scores = reciprocal_rank_fusion([["a", "b", "c"], ["c", "b", "d"]], k=60)

        order = sorted(scores, key=lambda d: -scores[d])
        assert order[:2] == ["b", "c"] or order[:2] == ["c", "b"]
        assert scores["b"] == pytest.approx(1 / 62 + 1 / 62)
        assert scores["a"] == pytest.approx(1 / 61)
        assert scores["d"] < scores["a"]

    def test_empty_rankings(self):
        assert reciprocal_rank_fusion([[], []]) == {}


class TestSync:
    async def test_sync_persists_event_timezone_from_get_event(self, config):
        client = FakeCatalogClient(
            catalog(),
            events=[
                Event(event_id=config.aws_events_event_id, name="Summit", timezone="Europe/London")
            ],
        )
        service = IngestionService(config, client, embedder=FakeEmbedder())
        await service.initial_sync()

        reopened = IngestionService(config, client, embedder=None)
        await reopened.initialize()
        assert await reopened.event_timezone() == "Europe/London"

    async def test_sync_does_not_persist_an_unknown_event_timezone(self, config):
        client = FakeCatalogClient(
            catalog(),
            events=[
                Event(event_id=config.aws_events_event_id, name="Summit", timezone="Not/AZone")
            ],
        )
        service = IngestionService(config, client, embedder=FakeEmbedder())
        await service.initial_sync()

        assert await service.event_timezone() is None

    async def test_event_timezone_is_none_before_any_sync(self, config, fake_client):
        fresh = IngestionService(config, fake_client, embedder=None)
        await fresh.initialize()
        assert await fresh.event_timezone() is None

    def test_content_hash_ignores_display_only_local_fields(self):
        plain = make_session("S1")
        shown = plain.model_copy(
            update={"start_local": "2026-12-01T10:00-08:00", "end_local": "2026-12-01T11:00-08:00"}
        )
        assert content_hash(plain) == content_hash(shown)

    async def test_initial_sync_populates_sqlite_and_vectors(self, service, config):
        assert db_rows(config, "SELECT COUNT(*) FROM sessions") == [(5,)]
        assert vector_row_count(config) == 5

    async def test_vectors_survive_restart(self, service, config, fake_client):
        restarted = IngestionService(config, fake_client, embedder=FakeEmbedder())
        await restarted.initialize()

        assert vector_row_count(config) == 5
        results = await restarted.search("bedrock", level=300)
        assert results[0].session_id == "AI301"

    async def test_vectors_store_model_id_and_text_hash(self, service, config):
        rows = db_rows(config, "SELECT model_id, text_hash, length(vector) FROM session_vectors")
        assert len(rows) == 5
        expected_len = (len(FakeEmbedder.VOCAB) + 1) * 4  # float32 bytes
        for model_id, text_hash, vector_len in rows:
            assert model_id == FakeEmbedder.model_id
            assert len(text_hash) == 64
            assert vector_len == expected_len

    async def test_malformed_vector_row_degrades_search_to_keyword_only(
        self, service, config, fake_client
    ):
        with sqlite3.connect(config.local_db_path) as db:
            db.execute(
                "UPDATE session_vectors SET vector = ? WHERE session_id = 'AI301'", (b"short",)
            )
            db.commit()

        restarted = IngestionService(config, fake_client, embedder=FakeEmbedder())
        await restarted.initialize()
        results = await restarted.search("bedrock")

        assert results  # keyword ranking still finds matches; one bad vector doesn't crash search

    async def test_vectors_rebuild_when_missing_and_embedder_is_available(
        self, service, config, fake_client
    ):
        with sqlite3.connect(config.local_db_path) as db:
            db.execute("DELETE FROM session_vectors")
            db.commit()

        restarted = IngestionService(config, fake_client, embedder=FakeEmbedder())
        await restarted.initialize()

        assert restarted.vector_search_available is True
        assert vector_row_count(config) == 5

    async def test_initial_sync_requires_configured_event(self, config):
        client = FakeCatalogClient(catalog(), events=[event("other")])
        ingestion = IngestionService(config, client, embedder=FakeEmbedder())

        with pytest.raises(SyncError):
            await ingestion.initial_sync()

    async def test_failed_walk_keeps_previous_catalog(self, service, config, fake_client):
        fake_client.fail_listing = True

        with pytest.raises(AWSEventsAPIError):
            await service.initial_sync()

        assert db_rows(config, "SELECT COUNT(*) FROM sessions") == [(5,)]

    async def test_incremental_sync_touches_only_changed_rows(self, service, config, fake_client):
        before = dict(db_rows(config, "SELECT session_id, updated_at FROM sessions"))
        fake_client.sessions["SEC301"] = fake_client.sessions["SEC301"].model_copy(
            update={"title": "Zero trust security, updated"}
        )
        fake_client.sessions["NEW201"] = make_session("NEW201", title="Serverless 201", level=200)
        del fake_client.sessions["SVL301"]
        time.sleep(0.001)

        changed = await service.incremental_sync()

        after = dict(db_rows(config, "SELECT session_id, updated_at FROM sessions"))
        assert changed == 3
        assert after["AI301"] == before["AI301"]
        assert after["SEC301"] != before["SEC301"]
        assert "NEW201" in after
        assert db_rows(config, "SELECT status FROM sessions WHERE session_id='SVL301'") == [
            ("CANCELLED",)
        ]
        assert vector_row_count(config) == 6

    async def test_incremental_sync_without_changes_is_a_no_op(self, service):
        assert await service.incremental_sync() == 0

    async def test_sync_single_refreshes_seats_without_reembedding(
        self, service, config, fake_client
    ):
        fake_client.sessions["AI301"] = fake_client.sessions["AI301"].model_copy(
            update={"status": "FULL", "seat_availability": "unavailable"}
        )
        hash_before = db_rows(
            config, "SELECT text_hash FROM session_vectors WHERE session_id = 'AI301'"
        )

        session = await service.sync_single("AI301")

        hash_after = db_rows(
            config, "SELECT text_hash FROM session_vectors WHERE session_id = 'AI301'"
        )
        assert session.status == "FULL"
        assert hash_after == hash_before
        assert (await service.get_session_local("AI301")).status == "FULL"

    async def test_restart_after_seat_only_sync_keeps_full_index(
        self, service, config, fake_client
    ):
        fake_client.sessions["AI301"] = fake_client.sessions["AI301"].model_copy(
            update={"seat_availability": "limited", "status": "FULL"}
        )
        await service.incremental_sync()

        restarted = IngestionService(config, fake_client, embedder=None)
        await restarted.initialize()

        assert restarted.vector_search_state == "available"
        assert vector_row_count(config) == 5

    async def test_store_without_existing_vectors_rebuilds_everything(
        self, service, config, fake_client
    ):
        with sqlite3.connect(config.local_db_path) as db:
            db.execute("DELETE FROM session_vectors")  # simulate a lost index
            db.commit()

        restarted = IngestionService(config, fake_client, embedder=None)
        await restarted.initialize()  # startup without a model: degraded, no rebuild
        assert restarted.vector_search_state == "degraded"
        restarted._vectors._embedder = FakeEmbedder()
        # sync_single does not re-run initialize(), so _store sees the missing vectors.
        fake_client.sessions["AI301"] = fake_client.sessions["AI301"].model_copy(
            update={"title": "Changed"}
        )
        await restarted.sync_single("AI301")

        assert restarted.vector_search_state == "available"
        assert vector_row_count(config) == 5

    async def test_sync_single_marks_vanished_session_canceled(self, service, fake_client):
        del fake_client.sessions["AI201"]

        assert await service.sync_single("AI201") is None
        assert (await service.get_session_local("AI201")).status == "CANCELLED"


class TestSearch:
    async def test_bedrock_level_300_is_relevant_and_fast(self, service):
        started = time.perf_counter()
        results = await service.search("Bedrock generative AI", level=300)
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert results[0].session_id == "AI301"
        assert all(r.level == 300 for r in results)
        assert elapsed_ms < 150

    async def test_only_open_sessions_are_returned(self, service):
        results = await service.search("bedrock", limit=20)

        assert "FULL301" not in {r.session_id for r in results}

    async def test_hero_only(self, service):
        results = await service.search("sessions", hero_only=True, limit=20)

        assert [r.session_id for r in results] == ["AI301"]

    async def test_venue_filter_matches_the_canonical_venue(self, service):
        results = await service.search("patterns", venue="Venetian", limit=20)

        assert "SVL301" in {r.session_id for r in results}
        assert all(r.venue_id == "Venetian" for r in results)

    async def test_venue_filter_matches_canonical_name_from_alias(self, config):
        # Regression guard: aliases resolve to the canonical venue before filtering.
        client = FakeCatalogClient(
            [
                make_session("V1", title="Bedrock", venue="Venetian"),
                make_session("W1", title="Bedrock", venue="Wynn"),
            ]
        )
        ingestion = IngestionService(config, client, embedder=FakeEmbedder())
        await ingestion.initial_sync()

        results = await ingestion.search("bedrock", venue="Palazzo", limit=5)

        assert [r.session_id for r in results] == ["V1"]

    async def test_venue_filter_falls_back_to_zone_and_room_for_unregistered_venues(self, config):
        client = FakeCatalogClient(
            [
                make_session(
                    "X1",
                    title="Bedrock",
                    venue="Unknown",
                    zone="Excel London",
                    venue_source="ROOM_PARSED",
                ),
                make_session("W1", title="Bedrock", venue="Wynn"),
            ]
        )
        ingestion = IngestionService(config, client, embedder=FakeEmbedder())
        await ingestion.initial_sync()

        results = await ingestion.search("bedrock", venue="Excel London", limit=5)

        assert [r.session_id for r in results] == ["X1"]

    @pytest.mark.parametrize(
        "fields, query",
        [
            # Regression guards: each LIKE arm of the unregistered-venue fallback matches alone.
            ({"venue": "Harbour Pavilion"}, "Harbour"),
            (
                {"venue": "Unknown", "zone": "Other Zone", "room_name": "Lakeside Hall 5"},
                "Lakeside",
            ),
        ],
        ids=["venue_id_arm", "room_name_arm"],
    )
    async def test_venue_fallback_matches_on_venue_id_or_room_name_alone(
        self, config, fields, query
    ):
        client = FakeCatalogClient(
            [
                make_session("X1", title="Bedrock", venue_source="ROOM_PARSED", **fields),
                make_session("W1", title="Bedrock", venue="Wynn"),
            ]
        )
        ingestion = IngestionService(config, client, embedder=FakeEmbedder())
        await ingestion.initial_sync()

        results = await ingestion.search("bedrock", venue=query, limit=5)

        assert [r.session_id for r in results] == ["X1"]

    async def test_session_type_and_topic_filters(self, config):
        client = FakeCatalogClient(
            [
                make_session("A", title="Bedrock agents", session_type="Workshop", topics=["AI"]),
                make_session(
                    "B", title="Bedrock agents", session_type="Breakout session", topics=["AI"]
                ),
                make_session(
                    "C", title="Bedrock agents", session_type="Workshop", topics=["Security"]
                ),
            ]
        )
        ingestion = IngestionService(config, client, embedder=FakeEmbedder())
        await ingestion.initial_sync()

        results = await ingestion.search("bedrock", session_type="workshop", topic="ai", limit=5)

        assert [r.session_id for r in results] == ["A"]

    async def test_venue_filter_escapes_like_wildcards(self, service):
        assert await service.search("bedrock", venue="%") == []

    async def test_scores_are_attached_and_descending(self, service):
        results = await service.search("bedrock security", limit=20)

        scores = [r.relevance_score for r in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.parametrize("limit", [0, 21])
    async def test_limit_is_validated(self, service, limit):
        with pytest.raises(ValueError):
            await service.search("bedrock", limit=limit)

    async def test_search_on_empty_catalog(self, config):
        ingestion = IngestionService(config, FakeCatalogClient([]), embedder=FakeEmbedder())
        await ingestion.initialize()

        assert await ingestion.search("anything") == []


async def test_schema_migration_adds_new_columns(config):
    with sqlite3.connect(config.local_db_path) as db:
        db.execute(
            "CREATE TABLE sessions (session_id VARCHAR(64) PRIMARY KEY, title TEXT NOT NULL,"
            " description TEXT, level INTEGER, track VARCHAR(100), venue_id VARCHAR(50) "
            "NOT NULL, room_name VARCHAR(100), start_time TIMESTAMP NOT NULL, end_time "
            "TIMESTAMP NOT NULL, capacity INTEGER, reserved_count INTEGER, status "
            "VARCHAR(20), speaker_names TEXT, is_hero_speaker BOOLEAN DEFAULT FALSE, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    ingestion = IngestionService(config, FakeCatalogClient([]), embedder=FakeEmbedder())

    await ingestion.initialize()
    await ingestion.upsert_session(make_session("X1", start=at(9)))

    columns = {row[1] for row in db_rows(config, "PRAGMA table_info(sessions)")}
    assert {"seat_availability", "is_reservable", "content_hash"} <= columns


async def test_new_session_fields_round_trip_through_catalog(config):
    session = make_session(
        "AIM301-R1",
        session_code="AIM301-R1",
        session_type="Workshop",
        topics=["Artificial Intelligence"],
        services=["Amazon Bedrock"],
        venue_source="REGISTRY",
        floor=2,
        repeat_group="AIM301",
    )
    ingestion = IngestionService(config, FakeCatalogClient([session]), embedder=FakeEmbedder())
    await ingestion.initial_sync()

    stored = await ingestion.get_session_local("AIM301-R1")
    assert stored is not None
    assert stored.topics == ["Artificial Intelligence"]
    assert stored.services == ["Amazon Bedrock"]
    assert (stored.session_type, stored.floor, stored.repeat_group) == ("Workshop", 2, "AIM301")
    assert stored.venue_source == "REGISTRY"


async def test_legacy_catalog_upgrades_in_place(config):
    with sqlite3.connect(config.local_db_path) as db:
        db.execute(
            "CREATE TABLE sessions (session_id VARCHAR(64) PRIMARY KEY, title TEXT NOT NULL, "
            "description TEXT, level INTEGER, track VARCHAR(100), venue_id VARCHAR(50) NOT NULL, "
            "room_name VARCHAR(100), start_time TIMESTAMP NOT NULL, end_time TIMESTAMP NOT NULL, "
            "capacity INTEGER, reserved_count INTEGER, status VARCHAR(20), speaker_names TEXT, "
            "is_hero_speaker BOOLEAN DEFAULT FALSE, updated_at TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO sessions (session_id, title, venue_id, start_time, end_time, status) "
            "VALUES ('OLD1', 'Old', 'Venetian', '2026-12-01T18:00:00+00:00', "
            "'2026-12-01T19:00:00+00:00', 'OPEN')"
        )

    client = FakeCatalogClient([make_session("OLD1", topics=["Security"])])
    ingestion = IngestionService(config, client, embedder=FakeEmbedder())
    await ingestion.initialize()
    legacy = await ingestion.get_session_local("OLD1")
    assert legacy is not None and legacy.topics == []

    changed = await ingestion.incremental_sync()
    upgraded = await ingestion.get_session_local("OLD1")
    assert changed >= 1
    assert upgraded is not None and upgraded.topics == ["Security"]


async def test_repeat_group_counts_ignore_cancelled_rows(config):
    client = FakeCatalogClient(
        [
            make_session("A1-R1", repeat_group="A1"),
            make_session("A1-R2", repeat_group="A1"),
            make_session("A1-R3", repeat_group="A1", status="CANCELLED"),
            make_session("B1-R1", repeat_group="B1"),
            make_session("C1-R1", repeat_group="C1"),
        ]
    )
    ingestion = IngestionService(config, client, embedder=FakeEmbedder())
    await ingestion.initial_sync()

    assert await ingestion.repeat_group_counts(["A1", "B1", "missing"]) == {"A1": 2, "B1": 1}
    assert await ingestion.repeat_group_counts([]) == {}
