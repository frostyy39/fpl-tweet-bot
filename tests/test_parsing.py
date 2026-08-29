import pytest

from fpl_bot.errors import DataValidationError
from fpl_bot.parsing import parse_fixtures, parse_teams


def test_team_parsing_rejects_incomplete_data() -> None:
    with pytest.raises(DataValidationError, match="short_name"):
        parse_teams([{"id": 1, "name": "Arsenal"}])


def test_fixture_parsing_rejects_wrong_event() -> None:
    payload = [{"id": 10, "event": 4, "team_h": 1, "team_a": 2}]

    with pytest.raises(DataValidationError, match="expected 3"):
        parse_fixtures(payload, expected_event_id=3)


def test_fixture_parsing_rejects_same_home_and_away_team() -> None:
    payload = [{"id": 10, "event": 3, "team_h": 1, "team_a": 1}]

    with pytest.raises(DataValidationError, match="same home and away"):
        parse_fixtures(payload, expected_event_id=3)


def test_fixture_parsing_accepts_empty_event_fixture_list() -> None:
    assert parse_fixtures([], expected_event_id=3) == ()
