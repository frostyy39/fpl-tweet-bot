from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fpl_bot.captain_models import CaptainProjection
from fpl_bot.captain_projection import FplReviewCsvProjectionSource, InMemoryCaptainProjectionSource
from fpl_bot.captain_service import build_captain_report
from fpl_bot.errors import (
    CaptainFixtureError,
    CaptainPlayerResolutionError,
    CaptainProjectionError,
)
from fpl_bot.models import Fixture, FplEvent, FplPlayer, Team
from fpl_bot.service import build_event_report

EVENT_ID = 4
DEADLINE_UTC = datetime(2026, 9, 12, 17, 30, tzinfo=UTC)


def teams() -> tuple[Team, ...]:
    return tuple(
        Team(team_id, f"Official Team {team_id}", f"T{team_id}") for team_id in range(1, 9)
    )


def players(*, ownership: dict[int, str] | None = None) -> tuple[FplPlayer, ...]:
    official_ownership = {
        1: "45.0",
        2: "35.0",
        3: "25.0",
        4: "12.0",
        5: "9.99",
        6: "5.0",
    }
    official_ownership.update(ownership or {})
    names = {1: "Haaland", 2: "Salah", 3: "Saka", 4: "Palmer", 5: "Wood", 6: "Rogers"}
    return tuple(
        FplPlayer(
            element_id, names[element_id], element_id, Decimal(official_ownership[element_id])
        )
        for element_id in range(1, 7)
    )


def sgw_fixtures() -> tuple[Fixture, ...]:
    return (
        Fixture(1, EVENT_ID, 1, 2, datetime(2026, 9, 12, 14, 0, tzinfo=UTC)),
        Fixture(2, EVENT_ID, 3, 4, datetime(2026, 9, 12, 14, 0, tzinfo=UTC)),
        Fixture(3, EVENT_ID, 5, 6, datetime(2026, 9, 12, 16, 30, tzinfo=UTC)),
        Fixture(4, EVENT_ID, 7, 8, datetime(2026, 9, 12, 19, 0, tzinfo=UTC)),
    )


def projections(
    ordered: tuple[tuple[int, str], ...] | None = None,
) -> tuple[CaptainProjection, ...]:
    values = ordered or (
        (1, "10.0"),
        (2, "9.0"),
        (3, "8.0"),
        (4, "7.0"),
        (5, "6.0"),
        (6, "5.0"),
    )
    return tuple(CaptainProjection(element_id, Decimal(points)) for element_id, points in values)


def report_for(fixtures: tuple[Fixture, ...]):
    event = FplEvent(EVENT_ID, "Gameweek 4", DEADLINE_UTC, False, True)
    return build_event_report(event, teams(), fixtures)


def build(
    *,
    projection_values: tuple[CaptainProjection, ...] | None = None,
    official_players: tuple[FplPlayer, ...] | None = None,
    fixtures: tuple[Fixture, ...] | None = None,
):
    official_fixtures = fixtures or sgw_fixtures()
    source = InMemoryCaptainProjectionSource({EVENT_ID: projection_values or projections()})
    return build_captain_report(
        report_for(official_fixtures),
        source,
        official_players or players(),
        teams(),
        official_fixtures,
    )


def test_orchestration_builds_target_rankings_and_canonical_tweet() -> None:
    result = build()

    assert result.captain_target_utc == datetime(2026, 9, 12, 15, 30, tzinfo=UTC)
    assert result.captain_target_london.isoformat() == "2026-09-12T16:30:00+01:00"
    assert result.event_code == "GW4"
    assert [selection.player.element_id for selection in result.top_three] == [1, 2, 3]
    assert result.differential.player.element_id == 5
    assert "🥇 Haaland v T2 (H) - 10.00" in result.tweet
    assert "🐴 Wood v T6 (H) - 6.00" in result.tweet


def test_equal_projections_preserve_stable_source_order() -> None:
    result = build(
        projection_values=projections(
            ((2, "10"), (1, "10"), (3, "9"), (4, "8"), (5, "7"), (6, "6"))
        )
    )

    assert [selection.player.element_id for selection in result.top_three] == [2, 1, 3]


@pytest.mark.parametrize(
    ("fourth_ownership", "expected_differential"),
    [("9.99", 4), ("10.00", 5), ("10.01", 5)],
)
def test_differential_uses_strict_official_ownership_threshold(
    fourth_ownership: str,
    expected_differential: int,
) -> None:
    result = build(official_players=players(ownership={4: fourth_ownership}))

    assert result.differential.player.element_id == expected_differential


def test_under_ten_top_three_are_not_reused_as_differential() -> None:
    result = build(
        official_players=players(ownership={1: "1.0", 2: "2.0", 3: "3.0", 4: "30.0", 5: "9.99"})
    )

    assert [selection.player.element_id for selection in result.top_three] == [1, 2, 3]
    assert result.differential.player.element_id == 5


def test_differential_scans_past_multiple_high_ownership_players() -> None:
    result = build(official_players=players(ownership={4: "40.0", 5: "20.0", 6: "0.5"}))

    assert result.differential.player.element_id == 6


def test_no_qualifying_differential_fails_explicitly() -> None:
    with pytest.raises(CaptainProjectionError, match="no qualifying differential"):
        build(official_players=players(ownership={4: "10", 5: "20", 6: "30"}))


def test_display_name_and_ownership_come_only_from_official_fpl_player() -> None:
    result = build(
        official_players=players(ownership={5: "1.25"}),
    )

    assert result.differential.player.web_name == "Wood"
    assert result.differential.player.selected_by_percent == Decimal("1.25")


def test_sgw_away_fixture_is_lowercase() -> None:
    result = build()

    assert "🥈 Salah v t1 (a) - 9.00" in result.tweet


def test_dgw_mixed_fixtures_render_in_chronological_order() -> None:
    fixtures = sgw_fixtures() + (
        Fixture(5, EVENT_ID, 4, 1, datetime(2026, 9, 13, 14, 0, tzinfo=UTC)),
    )

    result = build(fixtures=fixtures)

    assert result.event_code == "DGW4"
    assert "🥇 Haaland v T2 (H) & t4 (a) - 10.00" in result.tweet


def test_unusual_three_fixture_event_keeps_every_fixture_in_time_order() -> None:
    fixtures = sgw_fixtures() + (
        Fixture(6, EVENT_ID, 6, 1, datetime(2026, 9, 14, 14, 0, tzinfo=UTC)),
        Fixture(5, EVENT_ID, 4, 1, datetime(2026, 9, 13, 14, 0, tzinfo=UTC)),
    )

    result = build(fixtures=fixtures)

    assert "🥇 Haaland v T2 (H) & t4 (a) & t6 (a) - 10.00" in result.tweet


def test_unknown_projection_element_fails_instead_of_name_matching() -> None:
    invalid = projections(((99, "11"), (1, "10"), (2, "9"), (3, "8"), (4, "7"), (5, "6")))

    with pytest.raises(CaptainPlayerResolutionError, match="element 99"):
        build(projection_values=invalid)


def test_unknown_player_id_from_review_csv_fails_during_official_enrichment(tmp_path) -> None:
    path = tmp_path / "review.csv"
    path.write_text("ID,4_Pts\n99,11\n1,10\n2,9\n3,8\n4,7\n5,6\n", encoding="utf-8")

    with pytest.raises(CaptainPlayerResolutionError, match="element 99"):
        build_captain_report(
            report_for(sgw_fixtures()),
            FplReviewCsvProjectionSource(path),
            players(),
            teams(),
            sgw_fixtures(),
        )


def test_unknown_low_ranked_review_player_after_selected_differential_is_irrelevant(
    tmp_path,
) -> None:
    path = tmp_path / "review.csv"
    path.write_text("ID,4_Pts\n1,10\n2,9\n3,8\n5,7\n99,0.1\n", encoding="utf-8")

    report = build_captain_report(
        report_for(sgw_fixtures()),
        FplReviewCsvProjectionSource(path),
        players(),
        teams(),
        sgw_fixtures(),
    )

    assert report.differential.player.element_id == 5


def test_ranked_player_without_event_fixture_fails_explicitly() -> None:
    incomplete = tuple(fixture for fixture in sgw_fixtures() if fixture.fixture_id != 3)

    with pytest.raises(CaptainFixtureError, match="no fixture"):
        build(fixtures=incomplete)


def test_projection_source_failure_propagates_without_side_effects() -> None:
    source = InMemoryCaptainProjectionSource({})

    with pytest.raises(CaptainProjectionError, match="unavailable"):
        build_captain_report(report_for(sgw_fixtures()), source, players(), teams(), sgw_fixtures())
