from datetime import UTC, datetime

from fpl_bot.captain_dry_run import build_live_captain_report, render_captain_audit
from fpl_bot.captain_projection import FplReviewCsvProjectionSource


class FakeFplSource:
    def fetch_bootstrap_static(self):
        return {
            "events": [
                {
                    "id": 4,
                    "name": "Gameweek 4",
                    "deadline_time": "2026-09-12T12:30:00Z",
                    "is_current": False,
                    "is_next": True,
                }
            ],
            "teams": [
                {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
                for team_id in range(1, 21)
            ],
            "elements": [
                {
                    "id": player_id,
                    "web_name": f"Official {player_id}",
                    "team": player_id,
                    "selected_by_percent": ownership,
                }
                for player_id, ownership in (
                    (1, "40.0"),
                    (2, "30.0"),
                    (3, "20.0"),
                    (4, "10.0"),
                    (5, "9.99"),
                )
            ],
        }

    def fetch_event_fixtures(self, event_id: int):
        assert event_id == 4
        return [
            {
                "id": fixture_id,
                "event": 4,
                "team_h": home,
                "team_a": away,
                "kickoff_time": f"2026-09-{12 + fixture_id:02d}T14:00:00Z",
            }
            for fixture_id, (home, away) in enumerate(
                (
                    (1, 2),
                    (3, 4),
                    (5, 6),
                    (7, 8),
                    (9, 10),
                    (11, 12),
                    (13, 14),
                    (15, 16),
                    (17, 18),
                    (19, 20),
                ),
                start=1,
            )
        ]


def test_local_dry_run_uses_live_boundaries_and_prints_auditable_result(tmp_path) -> None:
    csv_path = tmp_path / "review.csv"
    csv_path.write_text(
        "ID,4_Pts\n1,10\n2,9\n3,8\n4,7\n5,6\n",
        encoding="utf-8",
    )

    report = build_live_captain_report(
        FakeFplSource(),
        FplReviewCsvProjectionSource(csv_path),
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    output = render_captain_audit(report)

    assert report.event_code == "GW4"
    assert report.differential.player.element_id == 5
    assert "ID=1; name=Official 1; projection=10.00; fixtures=T2 (H)" in output
    assert "official ownership=9.99%" in output
    assert "Captain target UTC: 2026-09-12T10:30:00+00:00" in output
    assert "Rendered tweet:\n🧢 CAPTAIN PICKS 🧢" in output
