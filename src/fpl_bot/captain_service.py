"""Pure orchestration for deterministic Captain recommendations."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from fpl_bot.captain_models import (
    CaptainFixture,
    CaptainProjection,
    CaptainReport,
    CaptainSelection,
)
from fpl_bot.captain_projection import CaptainProjectionSource
from fpl_bot.captain_timing import captain_target_london, captain_target_utc
from fpl_bot.captain_tweet import render_captain_tweet
from fpl_bot.errors import (
    CaptainFixtureError,
    CaptainPlayerResolutionError,
    CaptainProjectionError,
)
from fpl_bot.models import EventReport, Fixture, FplPlayer, Team

DIFFERENTIAL_OWNERSHIP_LIMIT = Decimal("10.0")


def build_captain_report(
    event_report: EventReport,
    projection_source: CaptainProjectionSource,
    players: Sequence[FplPlayer],
    teams: Sequence[Team],
    fixtures: Sequence[Fixture],
) -> CaptainReport:
    """Combine projection ranks with authoritative FPL identity, ownership, and fixtures."""
    event_id = event_report.event.event_id
    projections = tuple(projection_source.fetch_event_projections(event_id))
    ranked = _rank_projections(projections)
    player_by_id = _unique_players(players)
    team_by_id = _unique_teams(teams)
    fixtures_by_team = _event_fixtures_by_team(event_id, fixtures)
    if len(ranked) < 3:
        raise CaptainProjectionError("Captain projections must contain at least three players")

    top_selections = tuple(
        _enrich(projection, player_by_id, team_by_id, fixtures_by_team) for projection in ranked[:3]
    )
    top_three = (top_selections[0], top_selections[1], top_selections[2])
    differential = None
    for projection in ranked[3:]:
        selection = _enrich(projection, player_by_id, team_by_id, fixtures_by_team)
        if selection.player.selected_by_percent < DIFFERENTIAL_OWNERSHIP_LIMIT:
            differential = selection
            break
    if differential is None:
        raise CaptainProjectionError(
            "Captain projections contain no qualifying differential outside the top three"
        )

    target_utc = captain_target_utc(event_report.event.deadline_utc)
    tweet = render_captain_tweet(event_report.event_code, top_three, differential)
    return CaptainReport(
        event=event_report.event,
        captain_target_utc=target_utc,
        captain_target_london=captain_target_london(event_report.event.deadline_utc),
        event_code=event_report.event_code,
        top_three=top_three,
        differential=differential,
        tweet=tweet,
    )


def _rank_projections(
    projections: Sequence[CaptainProjection],
) -> tuple[CaptainProjection, ...]:
    if not projections:
        raise CaptainProjectionError("Captain projection source returned no players")
    if not all(isinstance(projection, CaptainProjection) for projection in projections):
        raise CaptainProjectionError("Captain projection source returned an invalid record")
    element_ids = [projection.element_id for projection in projections]
    if len(element_ids) != len(set(element_ids)):
        raise CaptainProjectionError("Captain projections contain duplicate FPL element IDs")
    return tuple(
        sorted(
            projections,
            key=lambda projection: projection.projected_points,
            reverse=True,
        )
    )


def _unique_players(players: Sequence[FplPlayer]) -> dict[int, FplPlayer]:
    player_by_id = {player.element_id: player for player in players}
    if len(player_by_id) != len(players):
        raise CaptainPlayerResolutionError("Authoritative FPL players contain duplicate IDs")
    return player_by_id


def _unique_teams(teams: Sequence[Team]) -> dict[int, Team]:
    team_by_id = {team.team_id: team for team in teams}
    if len(team_by_id) != len(teams):
        raise CaptainPlayerResolutionError("Authoritative FPL teams contain duplicate IDs")
    return team_by_id


def _event_fixtures_by_team(
    event_id: int,
    fixtures: Sequence[Fixture],
) -> dict[int, tuple[Fixture, ...]]:
    if any(fixture.event_id != event_id for fixture in fixtures):
        raise CaptainFixtureError("Captain fixtures must belong to the selected FPL event")
    ordered = sorted(fixtures, key=_fixture_order)
    team_fixtures: dict[int, list[Fixture]] = {}
    for fixture in ordered:
        team_fixtures.setdefault(fixture.home_team_id, []).append(fixture)
        team_fixtures.setdefault(fixture.away_team_id, []).append(fixture)
    return {team_id: tuple(items) for team_id, items in team_fixtures.items()}


def _fixture_order(fixture: Fixture) -> tuple[datetime, int]:
    return (fixture.kickoff_time_utc or datetime.max.replace(tzinfo=UTC), fixture.fixture_id)


def _enrich(
    projection: CaptainProjection,
    player_by_id: dict[int, FplPlayer],
    team_by_id: dict[int, Team],
    fixtures_by_team: dict[int, tuple[Fixture, ...]],
) -> CaptainSelection:
    try:
        player = player_by_id[projection.element_id]
    except KeyError:
        raise CaptainPlayerResolutionError(
            f"Projection element {projection.element_id} is absent from official FPL players"
        ) from None
    try:
        team = team_by_id[player.team_id]
    except KeyError:
        raise CaptainPlayerResolutionError(
            f"Official FPL player {player.element_id} references an unknown team"
        ) from None
    team_fixtures = fixtures_by_team.get(team.team_id, ())
    if not team_fixtures:
        raise CaptainFixtureError(
            f"Official FPL player {player.element_id} has no fixture in the selected event"
        )
    selection_fixtures = tuple(
        _selection_fixture(team.team_id, fixture, team_by_id) for fixture in team_fixtures
    )
    return CaptainSelection(
        player=player,
        team=team,
        projected_points=projection.projected_points,
        fixtures=selection_fixtures,
    )


def _selection_fixture(
    team_id: int,
    fixture: Fixture,
    team_by_id: dict[int, Team],
) -> CaptainFixture:
    is_home = fixture.home_team_id == team_id
    opponent_id = fixture.away_team_id if is_home else fixture.home_team_id
    try:
        opponent = team_by_id[opponent_id]
    except KeyError:
        raise CaptainFixtureError(
            f"Fixture {fixture.fixture_id} references an unknown opponent team"
        ) from None
    return CaptainFixture(opponent=opponent, is_home=is_home)
