from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from fpl_bot.deadline_planning import (
    DeadlinePlanner,
    DeadlinePlanningStatus,
    decide_london_deadline_day,
)
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.errors import DataValidationError, FplApiError, NoSuitableEventError
from fpl_bot.models import FplEvent


class FakeFplSource:
    def __init__(self, bootstrap: Mapping[str, Any] | Exception) -> None:
        self.bootstrap = bootstrap
        self.calls: list[str] = []

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        self.calls.append("bootstrap")
        if isinstance(self.bootstrap, Exception):
            raise self.bootstrap
        return self.bootstrap

    def fetch_event_fixtures(self, event_id: int) -> list[Mapping[str, Any]]:
        self.calls.append(f"fixtures:{event_id}")
        raise AssertionError("deadline planning must not fetch fixtures")


def event_payload(
    event_id: int,
    deadline: datetime | object,
    *,
    is_current: bool = False,
    is_next: bool = False,
) -> dict[str, object]:
    deadline_value = (
        deadline.isoformat().replace("+00:00", "Z") if isinstance(deadline, datetime) else deadline
    )
    return {
        "id": event_id,
        "name": f"Gameweek {event_id}",
        "deadline_time": deadline_value,
        "is_current": is_current,
        "is_next": is_next,
    }


def planner_for(
    *,
    now: datetime,
    events: list[dict[str, object]],
) -> tuple[DeadlinePlanner, FakeFplSource]:
    source = FakeFplSource({"events": events})
    return DeadlinePlanner(source, clock=lambda: now), source


@pytest.mark.parametrize(
    ("now", "deadline"),
    [
        (
            datetime(2026, 8, 29, 8, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 17, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 17, 8, 0, tzinfo=UTC),
            datetime(2026, 1, 17, 11, 0, tzinfo=UTC),
        ),
    ],
    ids=["bst", "gmt"],
)
def test_deadline_today_in_london_is_eligible(now: datetime, deadline: datetime) -> None:
    planner, _ = planner_for(
        now=now,
        events=[event_payload(3, deadline, is_next=True)],
    )

    decision = planner.plan()

    assert decision.status is DeadlinePlanningStatus.ELIGIBLE_TO_ARM
    assert decision.should_arm is True
    assert decision.instruction == ScheduledDeadlineInstruction(3, deadline)


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 29, 9, 55, tzinfo=UTC),
        datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        datetime(2026, 8, 29, 10, 5, tzinfo=UTC),
        datetime(2026, 8, 29, 22, 59, tzinfo=UTC),
    ],
    ids=["five-minutes-before", "exact-deadline", "five-minutes-after", "2359-london"],
)
def test_same_london_deadline_day_arms_without_lateness_cutoff(now: datetime) -> None:
    deadline = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    planner, _ = planner_for(
        now=now,
        events=[
            event_payload(3, deadline, is_current=True),
            event_payload(
                4,
                datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
                is_next=True,
            ),
        ],
    )

    decision = planner.plan()

    assert decision.should_arm is True
    assert decision.instruction == ScheduledDeadlineInstruction(3, deadline)


def test_london_midnight_next_day_no_longer_arms_prior_day_event() -> None:
    prior_deadline = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    future_deadline = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    planner, _ = planner_for(
        now=datetime(2026, 8, 29, 23, 0, tzinfo=UTC),
        events=[
            event_payload(3, prior_deadline, is_current=True),
            event_payload(4, future_deadline, is_next=True),
        ],
    )

    decision = planner.plan()

    assert decision.should_arm is False
    assert decision.instruction is None


def test_future_next_event_is_not_displaced_by_old_prior_day_event() -> None:
    now = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
    future_deadline = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    planner, _ = planner_for(
        now=now,
        events=[
            event_payload(3, datetime(2026, 8, 29, 10, 0, tzinfo=UTC), is_current=True),
            event_payload(4, future_deadline, is_next=True),
        ],
    )

    decision = planner.plan()

    assert decision.status is DeadlinePlanningStatus.NOT_CURRENT_LONDON_DAY
    assert decision.instruction is None


def test_deadline_tomorrow_in_london_is_not_eligible() -> None:
    planner, _ = planner_for(
        now=datetime(2026, 8, 29, 8, 0, tzinfo=UTC),
        events=[event_payload(3, datetime(2026, 8, 30, 10, 30, tzinfo=UTC))],
    )

    decision = planner.plan()

    assert decision.status is DeadlinePlanningStatus.NOT_CURRENT_LONDON_DAY
    assert decision.should_arm is False
    assert decision.instruction is None


def test_selected_deadline_yesterday_in_london_is_not_eligible() -> None:
    event = FplEvent(
        3,
        "Gameweek 3",
        datetime(2026, 8, 28, 17, 30, tzinfo=UTC),
        False,
        False,
    )

    decision = decide_london_deadline_day(
        event,
        datetime(2026, 8, 29, 8, 0, tzinfo=UTC),
    )

    assert decision.status is DeadlinePlanningStatus.NOT_CURRENT_LONDON_DAY


def test_different_utc_dates_but_same_london_date_is_eligible() -> None:
    now = datetime(2026, 8, 30, 23, 30, tzinfo=UTC)
    deadline = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
    planner, _ = planner_for(now=now, events=[event_payload(3, deadline)])

    assert planner.plan().should_arm is True


def test_same_utc_date_but_different_london_dates_is_not_eligible() -> None:
    now = datetime(2026, 8, 30, 22, 30, tzinfo=UTC)
    deadline = datetime(2026, 8, 30, 23, 30, tzinfo=UTC)
    planner, _ = planner_for(now=now, events=[event_payload(3, deadline)])

    assert planner.plan().should_arm is False


@pytest.mark.parametrize(
    ("now", "deadline", "expected"),
    [
        (
            datetime(2026, 8, 30, 22, 59, 59, tzinfo=UTC),
            datetime(2026, 8, 30, 23, 30, tzinfo=UTC),
            False,
        ),
        (
            datetime(2026, 8, 30, 23, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 30, 23, 30, tzinfo=UTC),
            True,
        ),
    ],
    ids=["immediately-before-london-midnight", "immediately-after-london-midnight"],
)
def test_london_midnight_boundary(
    now: datetime,
    deadline: datetime,
    expected: bool,
) -> None:
    planner, _ = planner_for(now=now, events=[event_payload(3, deadline)])

    assert planner.plan().should_arm is expected


@pytest.mark.parametrize(
    ("now", "deadline"),
    [
        (
            datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
            datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        ),
    ],
    ids=["spring-forward", "autumn-fall-back"],
)
def test_london_dst_transition_uses_timezone_database(
    now: datetime,
    deadline: datetime,
) -> None:
    planner, _ = planner_for(now=now, events=[event_payload(3, deadline)])

    assert planner.plan().should_arm is True


def test_instruction_preserves_authoritative_event_identity_exactly() -> None:
    deadline = datetime(2026, 8, 29, 10, 30, 45, 123456, tzinfo=UTC)
    planner, _ = planner_for(
        now=datetime(2026, 8, 29, 8, 0, tzinfo=UTC),
        events=[
            event_payload(2, datetime(2026, 8, 22, 10, 30, tzinfo=UTC), is_current=True),
            event_payload(17, deadline, is_next=True),
        ],
    )

    instruction = planner.plan().instruction

    assert instruction is not None
    assert instruction.expected_event_id == 17
    assert instruction.expected_deadline_utc == deadline
    assert [field.name for field in fields(instruction)] == [
        "expected_event_id",
        "expected_deadline_utc",
    ]


def test_existing_chronology_safe_event_selector_is_reused() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    planner, _ = planner_for(
        now=now,
        events=[
            event_payload(2, now + timedelta(days=1)),
            event_payload(3, now + timedelta(days=7), is_next=True),
        ],
    )

    with pytest.raises(DataValidationError, match="earlier unpassed deadline"):
        planner.plan()


@pytest.mark.parametrize(
    "bootstrap",
    [
        {},
        {"events": "not-an-array"},
        {"events": [event_payload(3, "not-a-deadline")]},
    ],
    ids=["missing-events", "malformed-events", "invalid-deadline"],
)
def test_malformed_bootstrap_fails_closed(bootstrap: Mapping[str, Any]) -> None:
    source = FakeFplSource(bootstrap)

    with pytest.raises(DataValidationError):
        DeadlinePlanner(source, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)).plan()

    assert source.calls == ["bootstrap"]


def test_unavailable_bootstrap_fails_closed() -> None:
    source = FakeFplSource(FplApiError("FPL unavailable"))

    with pytest.raises(FplApiError, match="unavailable"):
        DeadlinePlanner(source, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)).plan()

    assert source.calls == ["bootstrap"]


def test_no_valid_current_or_future_event_is_explicit_failure() -> None:
    now = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)
    planner, source = planner_for(
        now=now,
        events=[event_payload(2, now - timedelta(days=7), is_current=True)],
    )

    with pytest.raises(NoSuitableEventError, match="no event"):
        planner.plan()

    assert source.calls == ["bootstrap"]


def test_aware_non_utc_clock_is_normalized_before_selection_and_date_comparison() -> None:
    deadline = datetime(2026, 8, 29, 17, 30, tzinfo=UTC)
    planner, _ = planner_for(
        now=datetime(2026, 8, 29, 9, 0, tzinfo=timezone(timedelta(hours=1))),
        events=[event_payload(3, deadline)],
    )

    assert planner.plan().should_arm is True


def test_naive_clock_fails_closed_without_fixture_or_posting_activity() -> None:
    planner, source = planner_for(
        now=datetime(2026, 8, 29, 8, 0),
        events=[event_payload(3, datetime(2026, 8, 29, 17, 30, tzinfo=UTC))],
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        planner.plan()

    assert source.calls == ["bootstrap"]


def test_planning_reads_only_bootstrap_and_has_no_fixture_or_state_activity() -> None:
    planner, source = planner_for(
        now=datetime(2026, 8, 29, 8, 0, tzinfo=UTC),
        events=[event_payload(3, datetime(2026, 8, 29, 17, 30, tzinfo=UTC))],
    )

    planner.plan()

    assert source.calls == ["bootstrap"]
