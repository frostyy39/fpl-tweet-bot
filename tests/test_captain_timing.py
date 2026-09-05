from datetime import UTC, datetime, timedelta, timezone

import pytest

from fpl_bot.captain_timing import captain_target_london, captain_target_utc


def test_captain_target_is_exactly_two_hours_before_bst_deadline() -> None:
    deadline = datetime(2026, 9, 4, 17, 30, tzinfo=UTC)

    assert captain_target_utc(deadline) == datetime(2026, 9, 4, 15, 30, tzinfo=UTC)
    london = captain_target_london(deadline)
    assert london.isoformat() == "2026-09-04T16:30:00+01:00"
    assert london.tzname() == "BST"


def test_captain_target_uses_gmt_in_winter() -> None:
    deadline = datetime(2026, 12, 12, 11, 0, tzinfo=UTC)

    assert captain_target_utc(deadline) == datetime(2026, 12, 12, 9, 0, tzinfo=UTC)
    london = captain_target_london(deadline)
    assert london.isoformat() == "2026-12-12T09:00:00+00:00"
    assert london.tzname() == "GMT"


def test_captain_target_crosses_dst_transition_in_utc_not_wall_clock_time() -> None:
    deadline = datetime(2026, 3, 29, 2, 30, tzinfo=UTC)

    target = captain_target_utc(deadline)

    assert deadline - target == timedelta(hours=2)
    assert captain_target_london(deadline).isoformat() == "2026-03-29T00:30:00+00:00"


def test_captain_target_is_independent_of_execution_region() -> None:
    deadline = datetime(2026, 9, 4, 17, 30, tzinfo=UTC)

    regional_targets = {
        region: captain_target_utc(deadline) for region in ("europe-west2", "europe-west1", "local")
    }

    assert set(regional_targets.values()) == {datetime(2026, 9, 4, 15, 30, tzinfo=UTC)}


def test_captain_target_rejects_naive_or_non_utc_deadline() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        captain_target_utc(datetime(2026, 9, 4, 17, 30))
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        captain_target_utc(datetime(2026, 9, 4, 18, 30, tzinfo=timezone(timedelta(hours=1))))
