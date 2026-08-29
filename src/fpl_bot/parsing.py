"""Validation and conversion of raw FPL team and fixture records."""

from collections.abc import Mapping
from typing import Any

from fpl_bot.errors import DataValidationError
from fpl_bot.models import Fixture, Team

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
    return Fixture(fixture_id, event_id, home_team_id, away_team_id)


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
