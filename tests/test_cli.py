from datetime import UTC, datetime

from fpl_bot.cli import render_report
from fpl_bot.models import (
    EventKind,
    EventReport,
    FixtureClassification,
    FplEvent,
    Team,
    TeamFixtureCount,
)


def test_human_readable_report_contains_required_dry_run_fields() -> None:
    deadline_utc = datetime(2026, 8, 22, 10, 30, tzinfo=UTC)
    deadline_london = datetime.fromisoformat("2026-08-22T11:30:00+01:00")
    classification = FixtureClassification(
        kind=EventKind.BDGW,
        team_counts=(
            TeamFixtureCount(Team(1, "Team One", "ONE"), 2),
            TeamFixtureCount(Team(2, "Team Two", "TWO"), 1),
            TeamFixtureCount(Team(3, "Team Three", "THR"), 0),
        ),
    )
    report = EventReport(
        event=FplEvent(37, "Gameweek 37", deadline_utc, False, True),
        deadline_london=deadline_london,
        classification=classification,
        event_code="BDGW37",
        tweet="Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #BDGW37",
    )

    output = render_report(report)

    assert "Event ID: 37" in output
    assert "Official deadline UTC: 2026-08-22T10:30:00+00:00" in output
    assert "Deadline Europe/London: 2026-08-22T11:30:00+01:00" in output
    assert "Classification: BDGW" in output
    assert "Event code: BDGW37" in output
    assert "Blank teams: THR" in output
    assert "Double teams: ONE (2)" in output
    assert "ONE (Team One): 2" in output
    assert output.endswith("Good luck everyone 🔒🥳\n\n#FPL #FPLCommunity #BDGW37")
