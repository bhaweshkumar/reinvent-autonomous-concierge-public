"""SPEC-05: conflict detection and transit-buffer proposals (pure: no API client)."""

import inspect
from datetime import timedelta

import pytest

from reinvent_concierge.booking.changes import ChangeKind
from reinvent_concierge.booking.transit_blocks import is_concierge_transit
from reinvent_concierge.models import PersonalTimeBlock
from reinvent_concierge.schedule import ConflictManager
from reinvent_concierge.transit import TransitEngine
from tests.factories import at, make_schedule, make_session


@pytest.fixture
def manager():
    return ConflictManager(TransitEngine())


def mandalay_then_venetian(gap_minutes: int):
    a = make_session("S1", "Mandalay Bay", start=at(13), minutes=30)
    b = make_session("S2", "Venetian", start=a.end_time + timedelta(minutes=gap_minutes))
    return a, b


class TestDetectConflicts:
    def test_same_venue_produces_no_reports(self, manager):
        a = make_session("S1", "Venetian", start=at(10))
        b = make_session("S2", "Venetian", start=at(11))

        assert manager.detect_conflicts(make_schedule(a, b)) == []

    def test_sufficient_gap_suggests_buffer(self, manager):
        # Venetian <-> Wynn needs 19 min (10 matrix + 5 + 4 inside_walk); a 20-min gap fits.
        a = make_session("S1", "Venetian", start=at(10))
        b = make_session("S2", "Wynn", start=at(11, 20))

        [report] = manager.detect_conflicts(make_schedule(b, a))

        assert (report.session_a_id, report.session_b_id) == ("S1", "S2")
        assert report.suggested_action == "ADD_BUFFER"
        assert report.deficit_minutes == 0

    def test_mandalay_to_venetian_twenty_minutes_is_cancel_one(self, manager):
        """Mandalay Bay end=13:30, Venetian start=13:50 -> needs 45 (35 matrix + 2x5 walk)."""
        [report] = manager.detect_conflicts(make_schedule(*mandalay_then_venetian(20)))

        assert report.deficit_minutes == 25
        assert report.suggested_action == "CANCEL_ONE"

    def test_unknown_venue_only_flags_overlaps(self, manager):
        a = make_session("S1", "Online", start=at(10))
        b = make_session("S2", "Venetian", start=at(11, 10))
        c = make_session("S3", "Venetian", start=at(11, 30))

        [report] = manager.detect_conflicts(make_schedule(a, b, c))

        assert (report.session_a_id, report.suggested_action) == ("S2", "CANCEL_ONE")

    def test_empty_schedule(self, manager):
        assert manager.detect_conflicts(make_schedule()) == []

    def test_unknown_venue_overlap_is_flagged_as_a_low_confidence_estimate(self, manager):
        a = make_session("S1", "Online", start=at(10))
        b = make_session("S2", "Online", start=at(10, 30))  # overlaps a

        [report] = manager.detect_conflicts(make_schedule(a, b))

        assert report.suggested_action == "CANCEL_ONE"
        assert report.estimate_quality == "LOW"

    def test_known_venue_conflict_is_a_normal_confidence_estimate(self, manager):
        [report] = manager.detect_conflicts(make_schedule(*mandalay_then_venetian(20)))

        assert report.suggested_action == "CANCEL_ONE"
        assert report.estimate_quality == "NORMAL"


class TestProposeTransitBuffers:
    def test_takes_no_client_and_has_no_write_methods(self):
        assert list(inspect.signature(ConflictManager).parameters) == [
            "transit_engine",
            "reserved_seat_lead_minutes",
        ]
        for removed in (
            "inject_transit_buffer",
            "remove_transit_buffer",
            "resolve_all_conflicts",
            "cancel_conflicting",
            "swap_in",
            "_restore",
        ):
            assert not hasattr(ConflictManager, removed)

    def test_add_buffer_pair_gives_one_create_transit_change(self, manager):
        a, b = mandalay_then_venetian(60)

        [change], unresolved = manager.propose_transit_buffers(make_schedule(a, b))

        assert unresolved == []
        assert change.kind is ChangeKind.CREATE_TRANSIT
        assert change.title == "Transit: Mandalay Bay → Venetian"
        assert change.start_time == a.end_time
        assert change.end_time == a.end_time + timedelta(minutes=45)
        assert change.location == "Mandalay Bay"
        assert change.risk == "Creates a concierge-owned transit block."
        assert change.description == (
            "concierge-transit:S2:Travel 45 min from Mandalay Bay to Venetian for Session S2"
        )
        assert is_concierge_transit(
            PersonalTimeBlock(
                id="x",
                title=change.title,
                description=change.description,
                start_time=change.start_time,
                end_time=change.end_time,
            )
        )

    def test_existing_legacy_owned_buffer_at_same_start_gives_no_change(self, manager):
        a, b = mandalay_then_venetian(60)
        legacy = PersonalTimeBlock(
            id="pt-1",
            title="Transit: Mandalay Bay to Venetian",
            description="Travel 35 min from Mandalay Bay to Venetian for Session S2",
            start_time=a.end_time,
            end_time=a.end_time + timedelta(minutes=35),
        )

        changes, unresolved = manager.propose_transit_buffers(
            make_schedule(a, b, personal_time=[legacy])
        )

        assert changes == [] and unresolved == []

    def test_attendee_block_with_a_transit_title_is_not_ownership(self, manager):
        a, b = mandalay_then_venetian(60)
        attendee_block = PersonalTimeBlock(
            id="pt-1",
            title="Transit: Mandalay Bay → Venetian",
            description="my own note",
            start_time=a.end_time,
            end_time=a.end_time + timedelta(minutes=35),
        )

        changes, _ = manager.propose_transit_buffers(
            make_schedule(a, b, personal_time=[attendee_block])
        )

        assert len(changes) == 1

    def test_owned_buffer_at_another_start_still_gives_a_change(self, manager):
        a, b = mandalay_then_venetian(60)
        elsewhere = PersonalTimeBlock(
            id="pt-1",
            title="Transit: Mandalay Bay → Venetian",
            description="concierge-transit:S2:old",
            start_time=a.end_time + timedelta(minutes=5),
            end_time=a.end_time + timedelta(minutes=40),
        )

        changes, _ = manager.propose_transit_buffers(make_schedule(a, b, personal_time=[elsewhere]))

        assert len(changes) == 1

    def test_cancel_one_pair_is_returned_unresolved(self, manager):
        # Venetian->Wynn needs 19 (10 + 5 + 4); Wynn->Mandalay Bay needs 49 (40 + 4 + 5);
        # Mandalay Bay->Venetian needs 45 (35 + 5 + 5) and gets a 20-min gap, too short.
        a = make_session("S1", "Venetian", start=at(10))
        b = make_session("S2", "Wynn", start=at(11, 20))
        c = make_session("S3", "Mandalay Bay", start=at(13, 10), minutes=30)
        d = make_session("S4", "Venetian", start=at(14))

        changes, unresolved = manager.propose_transit_buffers(make_schedule(a, b, c, d))

        # Venetian->Wynn and Wynn->Mandalay Bay fit; Mandalay Bay->Venetian does not.
        assert [change.title for change in changes] == [
            "Transit: Venetian → Wynn",
            "Transit: Wynn → Mandalay Bay",
        ]
        assert [(u.session_a_id, u.suggested_action) for u in unresolved] == [("S3", "CANCEL_ONE")]
