import pytest
from conftest import make_fixture, make_teams

from fpl_bot.classification import classify_fixtures, render_event_code
from fpl_bot.errors import DataValidationError
from fpl_bot.models import EventKind


def test_regular_gameweek_classification() -> None:
    result = classify_fixtures(
        make_teams(),
        (make_fixture(1, 1, 2), make_fixture(2, 3, 4)),
    )

    assert result.kind is EventKind.GW
    assert result.blank_teams == ()
    assert result.double_teams == ()


def test_blank_gameweek_classification() -> None:
    result = classify_fixtures(make_teams(), (make_fixture(1, 1, 2),))

    assert result.kind is EventKind.BGW
    assert [item.team.team_id for item in result.blank_teams] == [3, 4]
    assert result.double_teams == ()


def test_double_gameweek_classification() -> None:
    result = classify_fixtures(
        make_teams(),
        (
            make_fixture(1, 1, 2),
            make_fixture(2, 3, 4),
            make_fixture(3, 1, 3),
        ),
    )

    assert result.kind is EventKind.DGW
    assert result.blank_teams == ()
    assert [item.team.team_id for item in result.double_teams] == [1, 3]


def test_blank_and_double_gameweek_classification() -> None:
    result = classify_fixtures(
        make_teams(),
        (make_fixture(1, 1, 2), make_fixture(2, 1, 3)),
    )

    assert result.kind is EventKind.BDGW
    assert [item.team.team_id for item in result.blank_teams] == [4]
    assert [item.team.team_id for item in result.double_teams] == [1]


def test_fixture_counting_initializes_every_team_at_zero() -> None:
    result = classify_fixtures(
        make_teams(5),
        (make_fixture(1, 1, 2), make_fixture(2, 1, 3)),
    )

    assert {item.team.team_id: item.fixture_count for item in result.team_counts} == {
        1: 2,
        2: 1,
        3: 1,
        4: 0,
        5: 0,
    }


def test_fixture_with_unknown_team_is_rejected() -> None:
    with pytest.raises(DataValidationError, match="unknown team ID 99"):
        classify_fixtures(make_teams(), (make_fixture(1, 1, 99),))


def test_zero_fixtures_are_rejected_instead_of_classified_as_bgw() -> None:
    with pytest.raises(DataValidationError, match="At least one event fixture"):
        classify_fixtures(make_teams(), ())


@pytest.mark.parametrize(
    ("event_id", "kind", "expected"),
    [
        (3, EventKind.GW, "GW3"),
        (29, EventKind.BGW, "BGW29"),
        (34, EventKind.DGW, "DGW34"),
        (37, EventKind.BDGW, "BDGW37"),
    ],
)
def test_event_code_generation(event_id: int, kind: EventKind, expected: str) -> None:
    assert render_event_code(event_id, kind) == expected


def test_event_code_rejects_invalid_event_id() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        render_event_code(0, EventKind.GW)
