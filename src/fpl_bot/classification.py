"""Deterministic fixture counting and GW/BGW/DGW/BDGW classification."""

from collections.abc import Sequence

from fpl_bot.errors import DataValidationError
from fpl_bot.models import (
    EventKind,
    Fixture,
    FixtureClassification,
    Team,
    TeamFixtureCount,
)


def classify_fixtures(
    teams: Sequence[Team],
    fixtures: Sequence[Fixture],
) -> FixtureClassification:
    if not teams:
        raise DataValidationError("At least one current Premier League team is required")

    team_by_id = {team.team_id: team for team in teams}
    if len(team_by_id) != len(teams):
        raise DataValidationError("Team IDs must be unique")
    counts = dict.fromkeys(team_by_id, 0)

    for fixture in fixtures:
        for team_id in (fixture.home_team_id, fixture.away_team_id):
            if team_id not in counts:
                raise DataValidationError(
                    f"Fixture {fixture.fixture_id} references unknown team ID {team_id}"
                )
            counts[team_id] += 1

    team_counts = tuple(
        TeamFixtureCount(team=team_by_id[team_id], fixture_count=counts[team_id])
        for team_id in sorted(team_by_id)
    )
    has_blank = any(item.fixture_count == 0 for item in team_counts)
    has_multiple = any(item.fixture_count > 1 for item in team_counts)

    if has_blank and has_multiple:
        kind = EventKind.BDGW
    elif has_blank:
        kind = EventKind.BGW
    elif has_multiple:
        kind = EventKind.DGW
    else:
        kind = EventKind.GW

    return FixtureClassification(kind=kind, team_counts=team_counts)


def render_event_code(event_id: int, kind: EventKind) -> str:
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise ValueError("event_id must be a positive integer")
    return f"{kind.value}{event_id}"
