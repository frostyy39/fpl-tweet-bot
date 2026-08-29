from datetime import UTC, datetime, timedelta

import pytest

from fpl_bot.errors import DataValidationError, NoSuitableEventError
from fpl_bot.events import parse_deadline, parse_events, select_next_event, to_london
from fpl_bot.models import FplEvent


def make_event(
    event_id: int,
    deadline: datetime,
    *,
    is_current: bool = False,
    is_next: bool = False,
) -> FplEvent:
    return FplEvent(event_id, f"Gameweek {event_id}", deadline, is_current, is_next)


def test_deadline_parsing_returns_timezone_aware_utc() -> None:
    result = parse_deadline("2026-08-22T10:30:00Z")

    assert result == datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
    assert result.tzinfo is UTC
    assert result.utcoffset() == timedelta(0)


def test_deadline_parsing_normalizes_an_explicit_offset() -> None:
    result = parse_deadline("2026-08-22T11:30:00+01:00")

    assert result == datetime(2026, 8, 22, 10, 30, tzinfo=UTC)


def test_deadline_parsing_rejects_naive_datetime() -> None:
    with pytest.raises(DataValidationError, match="UTC offset"):
        parse_deadline("2026-08-22T10:30:00")


def test_london_conversion_uses_gmt_in_winter() -> None:
    result = to_london(datetime(2026, 1, 17, 12, 0, tzinfo=UTC))

    assert result.hour == 12
    assert result.utcoffset() == timedelta(0)
    assert result.tzname() == "GMT"


def test_london_conversion_uses_bst_in_summer() -> None:
    result = to_london(datetime(2026, 8, 22, 12, 0, tzinfo=UTC))

    assert result.hour == 13
    assert result.utcoffset() == timedelta(hours=1)
    assert result.tzname() == "BST"


def test_next_event_prefers_fpl_is_next_flag() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    events = (
        make_event(2, now + timedelta(days=2)),
        make_event(3, now + timedelta(days=9), is_next=True),
    )

    assert select_next_event(events, now).event_id == 3


def test_next_event_uses_future_current_flag_when_no_next_flag_exists() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    events = (
        make_event(2, now + timedelta(days=2), is_current=True),
        make_event(3, now + timedelta(days=9)),
    )

    assert select_next_event(events, now).event_id == 2


def test_next_event_falls_back_to_earliest_future_deadline() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    events = (
        make_event(4, now + timedelta(days=10)),
        make_event(3, now + timedelta(days=3)),
        make_event(2, now - timedelta(seconds=1), is_current=True),
    )

    assert select_next_event(events, now).event_id == 3


def test_no_suitable_event_is_reported_gracefully() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    events = (make_event(1, now - timedelta(days=1), is_current=True),)

    with pytest.raises(NoSuitableEventError, match="no event"):
        select_next_event(events, now)


def test_next_event_rejects_naive_reference_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        select_next_event((), datetime(2026, 8, 1))


def test_malformed_event_data_is_rejected() -> None:
    with pytest.raises(DataValidationError, match="deadline_time"):
        parse_events(
            [
                {
                    "id": 1,
                    "name": "Gameweek 1",
                    "is_current": False,
                    "is_next": True,
                }
            ]
        )


def test_multiple_explicit_next_events_are_rejected() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    events = (
        make_event(2, now + timedelta(days=2), is_next=True),
        make_event(3, now + timedelta(days=9), is_next=True),
    )

    with pytest.raises(DataValidationError, match="multiple eligible"):
        select_next_event(events, now)
