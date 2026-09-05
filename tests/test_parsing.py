from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fpl_bot.errors import DataValidationError
from fpl_bot.parsing import parse_fixtures, parse_players, parse_teams


def make_team_payload(count: int) -> list[dict[str, object]]:
    return [
        {"id": team_id, "name": f"Team {team_id}", "short_name": f"T{team_id}"}
        for team_id in range(1, count + 1)
    ]


def test_team_parsing_rejects_incomplete_data() -> None:
    with pytest.raises(DataValidationError, match="short_name"):
        parse_teams([{"id": 1, "name": "Arsenal"}])


def test_team_parsing_rejects_fewer_than_twenty_teams() -> None:
    with pytest.raises(DataValidationError, match="exactly 20 unique teams; received 19"):
        parse_teams(make_team_payload(19))


def test_team_parsing_rejects_more_than_twenty_teams() -> None:
    with pytest.raises(DataValidationError, match="exactly 20 unique teams; received 21"):
        parse_teams(make_team_payload(21))


def test_fixture_parsing_rejects_wrong_event() -> None:
    payload = [{"id": 10, "event": 4, "team_h": 1, "team_a": 2}]

    with pytest.raises(DataValidationError, match="expected 3"):
        parse_fixtures(payload, expected_event_id=3)


def test_fixture_parsing_rejects_same_home_and_away_team() -> None:
    payload = [{"id": 10, "event": 3, "team_h": 1, "team_a": 1}]

    with pytest.raises(DataValidationError, match="same home and away"):
        parse_fixtures(payload, expected_event_id=3)


def test_fixture_parsing_rejects_empty_event_fixture_list() -> None:
    with pytest.raises(DataValidationError, match="at least one fixture"):
        parse_fixtures([], expected_event_id=3)


def test_player_parsing_uses_official_element_identity_name_team_and_ownership() -> None:
    players = parse_players(
        [{"id": 355, "web_name": "Haaland", "team": 13, "selected_by_percent": "52.40"}]
    )

    assert players[0].element_id == 355
    assert players[0].web_name == "Haaland"
    assert players[0].team_id == 13
    assert players[0].selected_by_percent == Decimal("52.40")


@pytest.mark.parametrize("ownership", [9.99, "NaN", "Infinity", "-0.1", "100.1", ""])
def test_player_parsing_rejects_noncanonical_or_out_of_range_ownership(
    ownership: object,
) -> None:
    with pytest.raises(DataValidationError, match="selected_by_percent"):
        parse_players(
            [{"id": 1, "web_name": "Player", "team": 1, "selected_by_percent": ownership}]
        )


def test_player_parsing_rejects_duplicate_element_ids() -> None:
    payload = {"id": 1, "web_name": "Player", "team": 1, "selected_by_percent": "5.0"}

    with pytest.raises(DataValidationError, match="duplicate element IDs"):
        parse_players([payload, payload])


def test_fixture_parsing_normalizes_optional_kickoff_time_to_utc() -> None:
    fixtures = parse_fixtures(
        [
            {
                "id": 10,
                "event": 3,
                "team_h": 1,
                "team_a": 2,
                "kickoff_time": "2026-09-12T16:30:00+01:00",
            }
        ],
        expected_event_id=3,
    )

    assert fixtures[0].kickoff_time_utc == datetime(2026, 9, 12, 15, 30, tzinfo=UTC)
