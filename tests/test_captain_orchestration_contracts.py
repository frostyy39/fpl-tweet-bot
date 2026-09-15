import ast
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from fpl_bot import captain_handoff as contracts
from fpl_bot import captain_orchestration_timing as timing_module
from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CaptainAssignment,
    CleanupStatus,
    CompletenessStatus,
    ProjectionHandoff,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_orchestration_timing import CaptainTiming


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def handoff():
    assignment = CaptainAssignment(
        UUID(int=1), UUID(int=2), 5, "GW5", CaptainTiming(utc("2026-09-18T17:30:00Z"))
    )
    return ProjectionHandoff(
        1,
        assignment,
        UUID(int=3),
        assignment.timing.target_utc,
        assignment.timing.target_utc + timedelta(seconds=30),
        (
            ProjectionRecord(1, "João Pedro", "CHE", Decimal("6.4800"), 10),
            ProjectionRecord(4, "Ødegaard", "ARS", Decimal("6.48")),
        ),
        RowCounts(4, 2, 2, 2),
        CompletenessStatus.COMPLETE,
        AuthenticationStatus.AUTHENTICATED,
        CleanupStatus.RELEASED,
    )


@pytest.mark.parametrize(
    "deadline,target,warmup",
    [
        ("2026-09-18T17:30:00Z", "2026-09-18T16:30:00+01:00", "2026-09-18T16:15:00+01:00"),
        ("2026-12-18T17:30:00Z", "2026-12-18T15:30:00+00:00", "2026-12-18T15:15:00+00:00"),
        ("2026-03-29T03:05:00Z", "2026-03-29T02:05:00+01:00", "2026-03-29T00:50:00+00:00"),
        ("2026-10-25T03:05:00Z", "2026-10-25T01:05:00+00:00", "2026-10-25T01:50:00+01:00"),
        ("2026-09-19T00:00:00Z", "2026-09-18T23:00:00+01:00", "2026-09-18T22:45:00+01:00"),
        ("2026-09-19T01:05:00Z", "2026-09-19T00:05:00+01:00", "2026-09-18T23:50:00+01:00"),
        ("2026-12-19T01:00:00Z", "2026-12-18T23:00:00+00:00", "2026-12-18T22:45:00+00:00"),
    ],
)
def test_london_calendar_and_dst(deadline, target, warmup):
    timing = CaptainTiming(utc(deadline))
    assert timing.target_london.isoformat() == target
    assert timing.warmup_london.isoformat() == warmup
    assert timing.release_utc == timing.target_utc
    assert timing.deadline_utc - timing.target_utc == timedelta(hours=2)
    assert timing.target_utc - timing.warmup_utc == timedelta(minutes=15)


@pytest.mark.parametrize(
    "offset,permitted",
    [
        (timedelta(microseconds=-1), False),
        (timedelta(0), True),
        (timedelta(minutes=5), True),
        (timedelta(minutes=5, microseconds=1), False),
    ],
)
def test_inclusive_attempt_and_acquisition_window(handoff, offset, permitted):
    timing = handoff.assignment.timing
    at = timing.target_utc + offset
    assert timing.permits_new_attempt(at) is permitted
    assert timing.permits_posting_acquisition(at) is permitted


def test_warmup_is_separate_from_posting_acquisition(handoff):
    timing = handoff.assignment.timing
    assert timing.permits_warmup(timing.warmup_utc)
    assert not timing.permits_posting_acquisition(timing.warmup_utc)
    assert not timing.permits_warmup(timing.release_utc)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-09-18T17:30:00Z",
        datetime(2026, 9, 18),
        3,
        utc("2026-09-18T17:30:00+01:00"),
    ],
)
def test_domain_requires_utc_datetimes(value):
    with pytest.raises(ValueError):
        CaptainTiming(value)


def test_immutable_roundtrip_utf8_order_digest(handoff):
    encoded = handoff.canonical_bytes()
    assert "João Pedro".encode() in encoded
    decoded = ProjectionHandoff.from_payload(json.loads(encoded.decode("utf-8")))
    assert decoded == handoff
    assert decoded.canonical_bytes() == encoded
    assert [r.source_ordinal for r in decoded.records] == [1, 4]
    assert decoded.records[0].projected_points == Decimal("6.48")
    assert len(handoff.payload_digest) == 64
    handoff.verify_digest(decoded.payload_digest)
    with pytest.raises(FrozenInstanceError):
        handoff.attempt_id = UUID(int=9)
    with pytest.raises(FrozenInstanceError):
        handoff.records[0].review_name = "changed"
    payload = handoff.to_payload()
    payload["records"][0]["review_name"] = "mutated copy"
    assert handoff.records[0].review_name == "João Pedro"


def test_equivalent_decimal_and_key_order_have_same_digest(handoff):
    payload = handoff.to_payload()
    payload["records"][0]["projected_points"] = "+6.4800"
    payload = dict(reversed(list(payload.items())))
    assert ProjectionHandoff.from_payload(payload).payload_digest == handoff.payload_digest
    changed = replace(handoff, attempt_id=UUID(int=9))
    with pytest.raises(ValueError):
        changed.verify_digest(handoff.payload_digest)


@pytest.mark.parametrize(
    "points",
    [
        "NaN",
        "Infinity",
        "-Infinity",
        "1e2",
        "1,5",
        "",
        " 7.0",
        "7.0 junk",
        7.0,
        None,
        True,
        "9" * 131,
    ],
)
def test_wire_decimal_validation(handoff, points):
    payload = handoff.to_payload()
    payload["records"][0]["projected_points"] = points
    with pytest.raises(ValueError):
        ProjectionHandoff.from_payload(payload)


@pytest.mark.parametrize(
    "points", [Decimal("NaN"), Decimal("Infinity"), 6.48, Decimal("1E100000"), Decimal("1E-100000")]
)
def test_domain_decimal_validation(points):
    with pytest.raises(ValueError):
        ProjectionRecord(1, "Player", "ARS", points)


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_schema_version(handoff, version):
    with pytest.raises(ValueError):
        replace(handoff, schema_version=version)


@pytest.mark.parametrize("field", ["tweet", "url", "cookies", "authorization", "browser_state"])
def test_extra_fields_rejected(handoff, field):
    payload = handoff.to_payload()
    payload[field] = "not allowed"
    with pytest.raises(ValueError, match="schema fields"):
        ProjectionHandoff.from_payload(payload)


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-18",
        "2026-09-18T15:30:00",
        "bad",
        "2026-02-30T15:30:00Z",
        "2026-09-18T15:30:00+01:00",
        None,
    ],
)
def test_wire_timestamp_validation(handoff, value):
    payload = handoff.to_payload()
    payload["acquisition_started_utc"] = value
    with pytest.raises(ValueError):
        ProjectionHandoff.from_payload(payload)


@pytest.mark.parametrize("field", ["target_utc", "warmup_utc", "release_utc", "expiry_utc"])
def test_assignment_rejects_inconsistent_derived_times(handoff, field):
    payload = handoff.assignment.to_payload()
    payload[field] = "2026-09-18T12:00:00Z"
    with pytest.raises(ValueError, match="inconsistent"):
        CaptainAssignment.from_payload(payload)


@pytest.mark.parametrize("code", ["GW5", "BGW5", "DGW5", "BDGW5"])
def test_classification_labels_supported_but_not_inferred(handoff, code):
    assignment = replace(handoff.assignment, event_code=code)
    assert CaptainAssignment.from_payload(assignment.to_payload()) == assignment


@pytest.mark.parametrize("code", ["GW4", "GW05", "gw5", "GW5\n", "OTHER5"])
def test_code_must_match_event(handoff, code):
    with pytest.raises(ValueError):
        replace(handoff.assignment, event_code=code)


@pytest.mark.parametrize(
    "change",
    [
        {"source_ordinal": 1},
        {"official_element_id": 10},
        {"review_name": "Joa\u0303o  Pedro", "review_team": "CHE"},
    ],
)
def test_duplicate_records_rejected(handoff, change):
    second = replace(handoff.records[1], **change)
    with pytest.raises(ValueError):
        replace(handoff, records=(handoff.records[0], second))


def test_reordering_and_mutable_records_rejected(handoff):
    with pytest.raises(ValueError):
        replace(handoff, records=tuple(reversed(handoff.records)))
    with pytest.raises(ValueError):
        replace(handoff, records=list(handoff.records))


def test_counts_and_incomplete_handoff(handoff):
    with pytest.raises(ValueError):
        RowCounts(5, 2, 2, 2)
    with pytest.raises(ValueError):
        replace(handoff, counts=RowCounts(4, 3, 1, 2))
    incomplete = replace(handoff, completeness=CompletenessStatus.PARTIAL)
    with pytest.raises(ValueError):
        incomplete.require_eligible(handoff.assignment, handoff.acquisition_ended_utc)


def test_eligibility_stale_and_time_boundaries(handoff):
    timing = handoff.assignment.timing
    handoff.require_eligible(handoff.assignment, timing.expiry_utc)
    instantaneous = replace(handoff, acquisition_ended_utc=timing.target_utc)
    instantaneous.require_eligible(handoff.assignment, timing.target_utc)
    at_expiry = replace(
        handoff, acquisition_started_utc=timing.expiry_utc, acquisition_ended_utc=timing.expiry_utc
    )
    at_expiry.require_eligible(handoff.assignment, timing.expiry_utc)
    with pytest.raises(ValueError):
        handoff.require_eligible(handoff.assignment, timing.expiry_utc + timedelta(microseconds=1))
    early = replace(handoff, acquisition_started_utc=timing.target_utc - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        early.require_eligible(handoff.assignment, handoff.acquisition_ended_utc)
    with pytest.raises(ValueError):
        handoff.require_eligible(
            replace(handoff.assignment, generation_id=UUID(int=4)), handoff.acquisition_ended_utc
        )
    with pytest.raises(ValueError):
        replace(handoff, acquisition_ended_utc=timing.target_utc - timedelta(seconds=1))


@pytest.mark.parametrize(
    "field,value",
    [
        ("authentication", AuthenticationStatus.REQUIRED),
        ("authentication", AuthenticationStatus.NOT_CONFIRMED),
        ("cleanup", CleanupStatus.UNCLEAN),
        ("cleanup", CleanupStatus.IN_USE),
        ("completeness", CompletenessStatus.UNKNOWN),
    ],
)
def test_failure_classifications_cannot_be_posted(handoff, field, value):
    result = replace(handoff, **{field: value})
    assert ProjectionHandoff.from_payload(result.to_payload()) == result
    with pytest.raises(ValueError):
        result.require_eligible(handoff.assignment, handoff.acquisition_ended_utc)


def test_pure_import_boundary():
    allowed = {
        "hashlib",
        "json",
        "re",
        "unicodedata",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "uuid",
        "zoneinfo",
        "fpl_bot.captain_timing",
        "fpl_bot.events",
        "fpl_bot.captain_orchestration_timing",
    }
    for module in (contracts, timing_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name in allowed for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module in allowed


@pytest.mark.parametrize("value", ["0", "-0.00", "-2.50", "1E-64", "1E63", "9" * 64])
def test_decimal_limits_roundtrip_without_rounding(value):
    record = ProjectionRecord(1, "Player", "ARS", Decimal(value))
    assert ProjectionRecord.from_payload(record.to_payload()) == record


@pytest.mark.parametrize(
    "changes",
    [
        {"source_ordinal": True},
        {"official_element_id": 0},
        {"review_name": ""},
        {"review_name": "name\nother"},
        {"review_name": "\ud800"},
        {"review_team": "ars"},
    ],
)
def test_record_invalid_fields(handoff, changes):
    with pytest.raises(ValueError):
        replace(handoff.records[0], **changes)


def test_missing_nested_or_invalid_identity_fields(handoff):
    payload = handoff.to_payload()
    del payload["records"][0]["review_name"]
    with pytest.raises(ValueError):
        ProjectionHandoff.from_payload(payload)
    with pytest.raises(ValueError):
        replace(handoff, attempt_id=UUID(int=0))
    with pytest.raises(ValueError):
        replace(handoff.assignment, event_id=True)
    with pytest.raises(ValueError):
        replace(handoff, counts=RowCounts(0, 0, 0, 0))


def test_late_future_and_changed_deadline_handoffs_rejected(handoff):
    expiry = handoff.assignment.timing.expiry_utc
    late = replace(handoff, acquisition_ended_utc=expiry + timedelta(microseconds=1))
    with pytest.raises(ValueError):
        late.require_eligible(handoff.assignment, expiry)
    changed = replace(
        handoff.assignment,
        timing=CaptainTiming(handoff.assignment.timing.deadline_utc + timedelta(hours=1)),
    )
    with pytest.raises(ValueError):
        handoff.require_eligible(changed, handoff.acquisition_ended_utc)
    with pytest.raises(ValueError):
        handoff.require_eligible(handoff.assignment, handoff.acquisition_started_utc)


def test_timing_rejects_overflow_and_naive_comparison(handoff):
    with pytest.raises(ValueError):
        CaptainTiming(datetime.min.replace(tzinfo=utc("2026-01-01T00:00:00Z").tzinfo))
    with pytest.raises(ValueError):
        handoff.assignment.timing.permits_new_attempt(datetime(2026, 9, 18))
