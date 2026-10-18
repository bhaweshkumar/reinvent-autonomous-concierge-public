"""SPEC-09 planner model and schedule fingerprint behavior."""

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from reinvent_concierge.models import AttendeeSchedule, PersonalTimeBlock
from reinvent_concierge.planner.models import (
    ApplyOperation,
    ApplyOperationState,
    ApplyRun,
    ApplyRunState,
    CanonicalPersonalTime,
    ScheduleSnapshot,
)
from tests.factories import UTC, at, make_session


def personal_time(
    personal_time_id: str = "pt-1",
    *,
    start_time: datetime | None = None,
    location: str | None = "Venetian",
) -> PersonalTimeBlock:
    start_time = start_time or at(12)
    return PersonalTimeBlock(
        id=personal_time_id,
        title="Lunch",
        start_time=start_time,
        end_time=start_time + timedelta(minutes=30),
        description="Break",
        location=location,
    )


def test_empty_schedule_has_stable_fingerprint():
    snapshot = ScheduleSnapshot.from_schedule("reinvent2026", AttendeeSchedule())

    assert snapshot.booked_session_ids == ()
    assert snapshot.favorite_session_ids == ()
    assert snapshot.personal_time == ()
    assert snapshot.fingerprint == (
        "4294a20f2cf1579bcc594622f151e217ea5e7d57dadd8c9d7e81c6d5d14aa4ae"
    )


def test_schedule_fingerprint_and_output_are_order_independent():
    first = make_session("A")
    second = make_session("B")
    early = personal_time("pt-1", start_time=at(10))
    late = personal_time("pt-2", start_time=at(14))
    forward = AttendeeSchedule(
        booked=[second, first], favorites=[first, second], personal_time=[late, early]
    )
    reverse = AttendeeSchedule(
        booked=[first, second], favorites=[second, first], personal_time=[early, late]
    )

    first_snapshot = ScheduleSnapshot.from_schedule("reinvent2026", forward)
    second_snapshot = ScheduleSnapshot.from_schedule("reinvent2026", reverse)

    assert first_snapshot.fingerprint == second_snapshot.fingerprint
    assert first_snapshot.booked_session_ids == ("A", "B")
    assert first_snapshot.favorite_session_ids == ("A", "B")
    assert tuple(item.personal_time_id for item in first_snapshot.personal_time) == (
        "pt-1",
        "pt-2",
    )


def test_direct_snapshot_canonicalizes_reversed_schedule_content():
    canonical = ScheduleSnapshot.from_schedule(
        "reinvent2026",
        AttendeeSchedule(
            booked=[make_session("A"), make_session("B")],
            favorites=[make_session("A"), make_session("B")],
            personal_time=[
                personal_time("pt-1", start_time=at(10)),
                personal_time("pt-2", start_time=at(14)),
            ],
        ),
    )

    direct = ScheduleSnapshot(
        event_id="reinvent2026",
        booked_session_ids=tuple(reversed(canonical.booked_session_ids)),
        favorite_session_ids=tuple(reversed(canonical.favorite_session_ids)),
        personal_time=tuple(reversed(canonical.personal_time)),
        fingerprint=canonical.fingerprint,
    )

    assert direct.booked_session_ids == canonical.booked_session_ids
    assert direct.favorite_session_ids == canonical.favorite_session_ids
    assert direct.personal_time == canonical.personal_time
    assert direct.fingerprint == canonical.fingerprint


def test_snapshot_json_validation_canonicalizes_reversed_schedule_content():
    canonical = ScheduleSnapshot.from_schedule(
        "reinvent2026",
        AttendeeSchedule(
            booked=[make_session("A"), make_session("B")],
            favorites=[make_session("A"), make_session("B")],
            personal_time=[
                personal_time("pt-1", start_time=at(10)),
                personal_time("pt-2", start_time=at(14)),
            ],
        ),
    )
    values = canonical.model_dump(mode="json")
    values["booked_session_ids"].reverse()
    values["favorite_session_ids"].reverse()
    values["personal_time"].reverse()

    restored = ScheduleSnapshot.model_validate_json(json.dumps(values))

    assert restored.booked_session_ids == canonical.booked_session_ids
    assert restored.favorite_session_ids == canonical.favorite_session_ids
    assert restored.personal_time == canonical.personal_time
    assert restored.fingerprint == canonical.fingerprint


def test_snapshot_rejects_fingerprint_computed_from_noncanonical_caller_order():
    canonical = ScheduleSnapshot.from_schedule(
        "reinvent2026",
        AttendeeSchedule(
            booked=[make_session("A"), make_session("B")],
            favorites=[make_session("A"), make_session("B")],
            personal_time=[
                personal_time("pt-1", start_time=at(10)),
                personal_time("pt-2", start_time=at(14)),
            ],
        ),
    )
    values = canonical.model_dump(mode="json")
    values["booked_session_ids"].reverse()
    values["favorite_session_ids"].reverse()
    values["personal_time"].reverse()
    caller_order_payload = {
        "booked_session_ids": values["booked_session_ids"],
        "favorite_session_ids": values["favorite_session_ids"],
        "personal_time": values["personal_time"],
    }
    values["fingerprint"] = hashlib.sha256(
        json.dumps(
            caller_order_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    assert values["fingerprint"] != canonical.fingerprint
    with pytest.raises(ValidationError, match="fingerprint does not match snapshot contents"):
        ScheduleSnapshot.model_validate_json(json.dumps(values))


def test_personal_time_meaningful_change_changes_fingerprint():
    original = AttendeeSchedule(personal_time=[personal_time(location="Venetian")])
    changed = AttendeeSchedule(personal_time=[personal_time(location="Wynn")])

    assert (
        ScheduleSnapshot.from_schedule("reinvent2026", original).fingerprint
        != ScheduleSnapshot.from_schedule("reinvent2026", changed).fingerprint
    )


def test_schedule_snapshot_rejects_wrong_fingerprint_on_all_validation_paths():
    snapshot = ScheduleSnapshot.from_schedule("reinvent2026", AttendeeSchedule())
    values = snapshot.model_dump()
    values["fingerprint"] = "wrong"

    with pytest.raises(ValidationError, match="fingerprint does not match snapshot contents"):
        ScheduleSnapshot(**values)
    with pytest.raises(ValidationError, match="fingerprint does not match snapshot contents"):
        ScheduleSnapshot.model_validate_json(
            snapshot.model_dump_json().replace(snapshot.fingerprint, "wrong")
        )


def test_schedule_snapshot_persisted_round_trip_excludes_capture_metadata_from_fingerprint():
    snapshot = ScheduleSnapshot.from_schedule("reinvent2026", AttendeeSchedule())
    values = snapshot.model_dump()
    values["snapshot_id"] = uuid4()
    values["captured_at"] = snapshot.captured_at + timedelta(days=1)

    restored = ScheduleSnapshot.model_validate(values)

    assert ScheduleSnapshot.model_validate_json(restored.model_dump_json()) == restored
    assert restored.fingerprint == snapshot.fingerprint


def test_schedule_snapshot_rejects_duplicate_personal_time_ids():
    schedule = AttendeeSchedule(
        personal_time=[
            personal_time("pt-1", start_time=at(10)),
            personal_time("pt-1", start_time=at(14)),
        ]
    )

    with pytest.raises(ValidationError, match="duplicate personal_time ID: pt-1"):
        ScheduleSnapshot.from_schedule("reinvent2026", schedule)


def test_direct_snapshot_rejects_duplicate_personal_time_ids():
    item = CanonicalPersonalTime(
        personal_time_id="pt-1",
        title="Lunch",
        start_time=at(10),
        end_time=at(11),
    )

    with pytest.raises(ValidationError, match="duplicate personal_time ID: pt-1"):
        ScheduleSnapshot(
            event_id="reinvent2026",
            booked_session_ids=(),
            favorite_session_ids=(),
            personal_time=(item, item),
            fingerprint="abc123",
        )


def test_equivalent_timezone_instants_have_same_fingerprint():
    utc_start = datetime(2026, 12, 1, 18, tzinfo=UTC)
    pacific = timezone(timedelta(hours=-8))
    local_start = datetime(2026, 12, 1, 10, tzinfo=pacific)

    utc_schedule = AttendeeSchedule(personal_time=[personal_time(start_time=utc_start)])
    local_schedule = AttendeeSchedule(personal_time=[personal_time(start_time=local_start)])

    assert (
        ScheduleSnapshot.from_schedule("reinvent2026", utc_schedule).fingerprint
        == ScheduleSnapshot.from_schedule("reinvent2026", local_schedule).fingerprint
    )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is unavailable")
def test_naive_personal_time_fingerprint_is_host_timezone_independent(monkeypatch):
    schedule = AttendeeSchedule(personal_time=[personal_time(start_time=datetime(2026, 12, 1, 10))])

    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "UTC")
        time.tzset()
        utc_fingerprint = ScheduleSnapshot.from_schedule("reinvent2026", schedule).fingerprint

        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        pacific_fingerprint = ScheduleSnapshot.from_schedule("reinvent2026", schedule).fingerprint
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()

    assert pacific_fingerprint == utc_fingerprint


def test_snapshot_collections_are_immutable_tuples():
    snapshot = ScheduleSnapshot.from_schedule(
        "reinvent2026",
        AttendeeSchedule(booked=[make_session()], personal_time=[personal_time()]),
    )

    assert isinstance(snapshot.booked_session_ids, tuple)
    assert isinstance(snapshot.favorite_session_ids, tuple)
    assert isinstance(snapshot.personal_time, tuple)
    with pytest.raises(AttributeError):
        snapshot.booked_session_ids.append("NEW")  # type: ignore[attr-defined]


def test_schedule_snapshot_and_nested_personal_time_fields_are_frozen():
    snapshot = ScheduleSnapshot.from_schedule(
        "reinvent2026",
        AttendeeSchedule(personal_time=[personal_time()]),
    )

    with pytest.raises(ValidationError, match="Instance is frozen"):
        snapshot.fingerprint = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Instance is frozen"):
        snapshot.personal_time[0].title = "Changed"  # type: ignore[misc]


def test_apply_state_literals_match_the_journal_contract():
    assert set(get_args(ApplyRunState)) == {
        "PREVIEWED",
        "APPROVED",
        "APPLYING",
        "VERIFYING",
        "APPLIED",
        "STALE",
        "PARTIAL",
        "COMPENSATING",
        "COMPENSATED",
        "COMPENSATION_INCOMPLETE",
        "FAILED",
    }
    assert set(get_args(ApplyOperationState)) == {
        "PENDING",
        "ATTEMPTING",
        "CONFIRMED",
        "REFUSED",
        "FAILED",
        "COMPENSATED",
        "COMPENSATION_FAILED",
    }


def test_apply_models_json_round_trip():
    profile_id = uuid4()
    run = ApplyRun(
        event_id="reinvent2026",
        profile_id=profile_id,
        snapshot_fingerprint="abc123",
    )
    operation = ApplyOperation(
        apply_run_id=run.run_id,
        order=1,
        operation_type="RESERVE",
        request={"session_ids": ["SEC301"], "destructive": False},
        result={"confirmed": ["SEC301"]},
    )

    assert ApplyRun.model_validate_json(run.model_dump_json()) == run
    assert ApplyOperation.model_validate_json(operation.model_dump_json()) == operation
    assert run.state == "PREVIEWED"
    assert operation.state == "PENDING"
    assert operation.order == 1
    assert run.created_at.utcoffset() == timedelta(0)
    assert run.updated_at.utcoffset() == timedelta(0)
    assert operation.created_at.utcoffset() == timedelta(0)
    assert operation.updated_at.utcoffset() == timedelta(0)


def test_planner_models_forbid_extra_fields_and_validate_assignment():
    with pytest.raises(ValidationError, match="state_typo"):
        ApplyRun(
            event_id="reinvent2026",
            profile_id=uuid4(),
            snapshot_fingerprint="abc123",
            state_typo="APPLIED",  # type: ignore[call-arg]
        )

    run = ApplyRun(
        event_id="reinvent2026",
        profile_id=uuid4(),
        snapshot_fingerprint="abc123",
    )
    with pytest.raises(ValidationError):
        run.state = "NOT_A_STATE"  # type: ignore[assignment]


def test_apply_model_fields_are_frozen():
    run = ApplyRun(
        event_id="reinvent2026",
        profile_id=uuid4(),
        snapshot_fingerprint="abc123",
    )
    operation = ApplyOperation(
        apply_run_id=run.run_id,
        order=1,
        operation_type="RESERVE",
        request={},
    )

    with pytest.raises(ValidationError, match="Instance is frozen"):
        run.state = "APPROVED"
    with pytest.raises(ValidationError, match="Instance is frozen"):
        operation.state = "ATTEMPTING"


@pytest.mark.parametrize(
    ("field_name", "payload", "key"),
    [
        ("request", {"Authorization": "secret"}, "Authorization"),
        ("request", {"nested": [{"ACCESS_TOKEN": "secret"}]}, "ACCESS_TOKEN"),
        ("result", {"nested": ({"refresh_token": "secret"},)}, "refresh_token"),
        ("result", {"id_TOKEN": "secret"}, "id_TOKEN"),
        ("request", {"token": "secret"}, "token"),
        ("request", {"nested": [{"token_hash": "secret"}]}, "token_hash"),
        ("error", {"context": {"API_Key": "secret"}}, "API_Key"),
    ],
)
def test_apply_operation_rejects_sensitive_mapping_keys(field_name, payload, key):
    values = {
        "apply_run_id": uuid4(),
        "order": 1,
        "operation_type": "RESERVE",
        "request": {},
        field_name: payload,
    }

    with pytest.raises(ValidationError, match=f"sensitive key is not allowed: {key}"):
        ApplyOperation(**values)


def test_apply_operation_allows_ordinary_keys_and_sensitive_words_as_values():
    operation = ApplyOperation(
        apply_run_id=uuid4(),
        order=1,
        operation_type="RESERVE",
        request={
            "token_count": 3,
            "headers": [{"label": "authorization", "value": "token"}],
        },
        result={"status": "access_token"},
        error="api_key was unavailable",
    )

    assert operation.request["token_count"] == 3
    assert operation.result == {"status": "access_token"}
    assert operation.error == "api_key was unavailable"


@pytest.mark.parametrize("serialization_method", ["model_dump", "model_dump_json"])
@pytest.mark.parametrize("field_name", ["request", "result"])
@pytest.mark.parametrize("sensitive_key", ["access_token", "token_hash"])
def test_apply_operation_rejects_sensitive_keys_added_before_serialization(
    serialization_method, field_name, sensitive_key
):
    operation = ApplyOperation(
        apply_run_id=uuid4(),
        order=1,
        operation_type="RESERVE",
        request={"nested": []},
        result={"nested": []},
    )
    payload = getattr(operation, field_name)
    payload["nested"].append({"deeper": {sensitive_key: "secret"}})

    with pytest.raises(ValueError, match=f"sensitive key is not allowed: {sensitive_key}"):
        getattr(operation, serialization_method)()


@pytest.mark.parametrize("serialization_method", ["model_dump", "model_dump_json"])
def test_apply_operation_mutable_ordinary_payload_serializes_and_round_trips(
    serialization_method,
):
    operation = ApplyOperation(
        apply_run_id=uuid4(),
        order=1,
        operation_type="RESERVE",
        request={"nested": []},
        result={"confirmed": []},
    )
    request_nested = operation.request["nested"]
    assert isinstance(request_nested, list)
    request_nested.append({"session_ids": ["SEC301"]})
    assert operation.result is not None
    confirmed = operation.result["confirmed"]
    assert isinstance(confirmed, list)
    confirmed.append("SEC301")

    serialized = getattr(operation, serialization_method)()
    restored = (
        ApplyOperation.model_validate(serialized)
        if serialization_method == "model_dump"
        else ApplyOperation.model_validate_json(serialized)
    )

    assert restored == operation


@pytest.mark.parametrize(
    ("model", "field_names"),
    [
        (
            CanonicalPersonalTime(
                personal_time_id="pt-1",
                title="Lunch",
                start_time=datetime(2026, 12, 1, 10, tzinfo=timezone(timedelta(hours=-8))),
                end_time=datetime(2026, 12, 1, 11, tzinfo=timezone(timedelta(hours=-8))),
            ),
            ("start_time", "end_time"),
        ),
        (
            ScheduleSnapshot(
                event_id="reinvent2026",
                captured_at=datetime(2026, 12, 1, 10, tzinfo=timezone(timedelta(hours=-8))),
                booked_session_ids=(),
                favorite_session_ids=(),
                personal_time=(),
                fingerprint="4294a20f2cf1579bcc594622f151e217ea5e7d57dadd8c9d7e81c6d5d14aa4ae",
            ),
            ("captured_at",),
        ),
        (
            ApplyRun(
                event_id="reinvent2026",
                profile_id=uuid4(),
                snapshot_fingerprint="abc123",
                created_at=datetime(2026, 12, 1, 10, tzinfo=timezone(timedelta(hours=-8))),
                updated_at=datetime(2026, 12, 1, 11, tzinfo=timezone(timedelta(hours=-8))),
            ),
            ("created_at", "updated_at"),
        ),
        (
            ApplyOperation(
                apply_run_id=uuid4(),
                order=1,
                operation_type="RESERVE",
                request={},
                created_at=datetime(2026, 12, 1, 10, tzinfo=timezone(timedelta(hours=-8))),
                updated_at=datetime(2026, 12, 1, 11, tzinfo=timezone(timedelta(hours=-8))),
            ),
            ("created_at", "updated_at"),
        ),
    ],
)
def test_caller_supplied_aware_datetimes_are_normalized_to_utc(model, field_names):
    for field_name in field_names:
        value = getattr(model, field_name)
        assert value.tzinfo is timezone.utc
        assert value.hour == (19 if field_name in {"end_time", "updated_at"} else 18)


@pytest.mark.parametrize(
    ("model", "field_names"),
    [
        (
            CanonicalPersonalTime(
                personal_time_id="pt-1",
                title="Lunch",
                start_time=datetime(2026, 12, 1, 10),
                end_time=datetime(2026, 12, 1, 11),
            ),
            ("start_time", "end_time"),
        ),
        (
            ScheduleSnapshot(
                event_id="reinvent2026",
                captured_at=datetime(2026, 12, 1, 10),
                booked_session_ids=(),
                favorite_session_ids=(),
                personal_time=(),
                fingerprint="4294a20f2cf1579bcc594622f151e217ea5e7d57dadd8c9d7e81c6d5d14aa4ae",
            ),
            ("captured_at",),
        ),
        (
            ApplyRun(
                event_id="reinvent2026",
                profile_id=uuid4(),
                snapshot_fingerprint="abc123",
                created_at=datetime(2026, 12, 1, 10),
                updated_at=datetime(2026, 12, 1, 11),
            ),
            ("created_at", "updated_at"),
        ),
        (
            ApplyOperation(
                apply_run_id=uuid4(),
                order=1,
                operation_type="RESERVE",
                request={},
                created_at=datetime(2026, 12, 1, 10),
                updated_at=datetime(2026, 12, 1, 11),
            ),
            ("created_at", "updated_at"),
        ),
    ],
)
def test_caller_supplied_naive_datetimes_are_interpreted_as_utc(model, field_names):
    for field_name in field_names:
        value = getattr(model, field_name)
        assert value.tzinfo is timezone.utc
        assert value.hour == (11 if field_name in {"end_time", "updated_at"} else 10)


def test_schedule_snapshot_json_round_trip_with_personal_time():
    snapshot = ScheduleSnapshot.from_schedule(
        "reinvent2026",
        AttendeeSchedule(
            booked=[make_session("SEC301")],
            favorites=[make_session("SEC401")],
            personal_time=[personal_time()],
        ),
    )

    restored = ScheduleSnapshot.model_validate_json(snapshot.model_dump_json())

    assert restored == snapshot
    assert isinstance(restored.personal_time[0], CanonicalPersonalTime)
