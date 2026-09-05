"""Validation and conversion of raw FPL team and fixture records."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from fpl_bot.errors import DataValidationError
from fpl_bot.events import parse_deadline
from fpl_bot.models import Fixture, FplPlayer, Team

EXPECTED_TEAM_COUNT = 20


def parse_teams(payload: object) -> tuple[Team, ...]:
    if not isinstance(payload, list):
        raise DataValidationError("FPL teams must be a JSON array")

    teams = tuple(_parse_team(item) for item in payload)
    team_ids = [team.team_id for team in teams]
    if len(team_ids) != len(set(team_ids)):
        raise DataValidationError("FPL teams contain duplicate team IDs")
    if len(teams) != EXPECTED_TEAM_COUNT:
        raise DataValidationError(
            f"FPL teams must contain exactly {EXPECTED_TEAM_COUNT} unique teams; "
            f"received {len(teams)}"
        )
    return teams


def parse_fixtures(payload: object, expected_event_id: int) -> tuple[Fixture, ...]:
    if not isinstance(payload, list):
        raise DataValidationError("FPL fixtures must be a JSON array")
    if not payload:
        raise DataValidationError("FPL event fixtures must contain at least one fixture")

    fixtures = tuple(_parse_fixture(item, expected_event_id) for item in payload)
    fixture_ids = [fixture.fixture_id for fixture in fixtures]
    if len(fixture_ids) != len(set(fixture_ids)):
        raise DataValidationError("FPL fixtures contain duplicate fixture IDs")
    return fixtures


def parse_players(payload: object) -> tuple[FplPlayer, ...]:
    if not isinstance(payload, list):
        raise DataValidationError("FPL players must be a JSON array")

    players = tuple(_parse_player(item) for item in payload)
    element_ids = [player.element_id for player in players]
    if len(element_ids) != len(set(element_ids)):
        raise DataValidationError("FPL players contain duplicate element IDs")
    return players


def _parse_team(payload: object) -> Team:
    if not isinstance(payload, Mapping):
        raise DataValidationError("Each FPL team must be a JSON object")
    team_id = _positive_int(payload, "id", "team")
    return Team(
        team_id=team_id,
        name=_non_empty_string(payload, "name", f"Team {team_id}"),
        short_name=_non_empty_string(payload, "short_name", f"Team {team_id}"),
    )


def _parse_fixture(payload: object, expected_event_id: int) -> Fixture:
    if not isinstance(payload, Mapping):
        raise DataValidationError("Each FPL fixture must be a JSON object")
    fixture_id = _positive_int(payload, "id", "fixture")
    event_id = _positive_int(payload, "event", f"fixture {fixture_id}")
    if event_id != expected_event_id:
        raise DataValidationError(
            f"Fixture {fixture_id} belongs to event {event_id}, expected {expected_event_id}"
        )

    home_team_id = _positive_int(payload, "team_h", f"fixture {fixture_id}")
    away_team_id = _positive_int(payload, "team_a", f"fixture {fixture_id}")
    if home_team_id == away_team_id:
        raise DataValidationError(f"Fixture {fixture_id} has the same home and away team")
    kickoff_value = payload.get("kickoff_time")
    kickoff_time_utc = None if kickoff_value is None else parse_deadline(kickoff_value)
    return Fixture(fixture_id, event_id, home_team_id, away_team_id, kickoff_time_utc)


def _parse_player(payload: object) -> FplPlayer:
    if not isinstance(payload, Mapping):
        raise DataValidationError("Each FPL player must be a JSON object")
    element_id = _positive_int(payload, "id", "player")
    ownership_value = payload.get("selected_by_percent")
    if not isinstance(ownership_value, str) or not ownership_value.strip():
        raise DataValidationError(
            f"Player {element_id} field selected_by_percent must be a decimal string"
        )
    try:
        ownership = Decimal(ownership_value.strip())
    except InvalidOperation as exc:
        raise DataValidationError(
            f"Player {element_id} field selected_by_percent must be a decimal string"
        ) from exc
    if not ownership.is_finite() or ownership < 0 or ownership > 100:
        raise DataValidationError(
            f"Player {element_id} selected_by_percent must be between 0 and 100"
        )
    return FplPlayer(
        element_id=element_id,
        web_name=_non_empty_string(payload, "web_name", f"Player {element_id}"),
        team_id=_positive_int(payload, "team", f"player {element_id}"),
        selected_by_percent=ownership,
    )


def _positive_int(payload: Mapping[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DataValidationError(f"FPL {label} field {key} must be a positive integer")
    return value


def _non_empty_string(payload: Mapping[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError(f"{label} field {key} must be a non-empty string")
    return value.strip()
