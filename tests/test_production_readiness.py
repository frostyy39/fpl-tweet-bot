from datetime import UTC, datetime

import pytest

from fpl_bot.production_readiness import (
    ProductionTargetMismatch,
    main,
    verify_production_target,
)


class Source:
    def __init__(self, *, event_id=6, deadline="2026-10-10T10:00:00Z"):
        self.event_id, self.deadline = event_id, deadline

    def fetch_bootstrap_static(self):
        return {
            "events": [
                {
                    "id": self.event_id,
                    "name": f"Gameweek {self.event_id}",
                    "deadline_time": self.deadline,
                    "is_next": True,
                    "is_current": False,
                    "finished": False,
                }
            ],
            "teams": [
                {"id": value, "name": f"Team {value}", "short_name": f"T{value:02}"}
                for value in range(1, 21)
            ],
        }

    def fetch_event_fixtures(self, event_id):
        return [
            {
                "id": value,
                "event": event_id,
                "team_h": value,
                "team_a": value + 10,
                "kickoff_time": "2026-10-10T14:00:00Z",
            }
            for value in range(1, 11)
        ]


DEADLINE = datetime(2026, 10, 10, 10, tzinfo=UTC)


def test_fresh_target_derives_exact_captain_and_good_luck_boundaries():
    target = verify_production_target(Source(), expected_event_id=6, expected_deadline_utc=DEADLINE)
    assert target.payload() == {
        "event_id": 6,
        "event_code": "GW6",
        "official_deadline_utc": "2026-10-10T10:00:00.000000Z",
        "captain_warmup_utc": "2026-10-10T07:45:00.000000Z",
        "captain_target_utc": "2026-10-10T08:00:00.000000Z",
        "captain_expiry_utc": "2026-10-10T08:05:00.000000Z",
    }


@pytest.mark.parametrize(
    "source,event_id,deadline",
    [
        (Source(event_id=7), 6, DEADLINE),
        (Source(deadline="2026-10-10T10:30:00Z"), 6, DEADLINE),
    ],
)
def test_fresh_target_rejects_changed_event_or_deadline(source, event_id, deadline):
    with pytest.raises(ProductionTargetMismatch):
        verify_production_target(source, expected_event_id=event_id, expected_deadline_utc=deadline)


def test_cli_fails_closed_before_fetch_for_invalid_deadline(capsys):
    assert main(["--expected-event-id", "6", "--expected-deadline-utc", "not-a-date"]) == 1
    assert capsys.readouterr().out.strip() == '{"status": "stale_or_invalid_target"}'
