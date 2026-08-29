from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from fpl_bot.errors import DataValidationError
from fpl_bot.models import EventKind
from fpl_bot.service import build_next_event_report


class FakeFplDataSource:
    def __init__(
        self,
        bootstrap: Mapping[str, Any],
        fixtures: Sequence[Mapping[str, Any]],
    ) -> None:
        self.bootstrap = bootstrap
        self.fixtures = fixtures
        self.requested_event_id: int | None = None

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        return self.bootstrap

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        self.requested_event_id = event_id
        return self.fixtures


def test_service_builds_report_from_mocked_fpl_responses() -> None:
    source = FakeFplDataSource(
        bootstrap={
            "events": [
                {
                    "id": 3,
                    "name": "Gameweek 3",
                    "deadline_time": "2026-08-22T10:30:00Z",
                    "is_current": False,
                    "is_next": True,
                }
            ],
            "teams": [
                {"id": 1, "name": "Team 1", "short_name": "T1"},
                {"id": 2, "name": "Team 2", "short_name": "T2"},
                {"id": 3, "name": "Team 3", "short_name": "T3"},
                {"id": 4, "name": "Team 4", "short_name": "T4"},
            ],
        },
        fixtures=[
            {"id": 1, "event": 3, "team_h": 1, "team_a": 2},
            {"id": 2, "event": 3, "team_h": 3, "team_a": 4},
        ],
    )

    report = build_next_event_report(
        source,
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert source.requested_event_id == 3
    assert report.event.event_id == 3
    assert report.classification.kind is EventKind.GW
    assert report.event_code == "GW3"
    assert report.deadline_london.isoformat() == "2026-08-22T11:30:00+01:00"
    assert report.tweet == "Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #GW3"


def test_service_rejects_incomplete_bootstrap_response() -> None:
    source = FakeFplDataSource(bootstrap={"events": []}, fixtures=[])

    with pytest.raises(DataValidationError, match="events and teams"):
        build_next_event_report(source, now=datetime(2026, 8, 1, tzinfo=UTC))
