from decimal import Decimal

import pytest

from fpl_bot.captain_models import CaptainFixture, CaptainSelection
from fpl_bot.captain_tweet import (
    X_POST_WEIGHTED_LIMIT,
    format_projection,
    render_captain_tweet,
    x_weighted_text_length,
)
from fpl_bot.errors import CaptainRenderingError
from fpl_bot.models import FplPlayer, Team


def selection(
    element_id: int,
    name: str,
    points: str,
    *fixtures: tuple[str, bool],
) -> CaptainSelection:
    team = Team(element_id, f"Team {element_id}", f"T{element_id}")
    return CaptainSelection(
        player=FplPlayer(element_id, name, team.team_id, Decimal("20.0")),
        team=team,
        projected_points=Decimal(points),
        fixtures=tuple(
            CaptainFixture(Team(100 + index, short_name, short_name), is_home)
            for index, (short_name, is_home) in enumerate(fixtures)
        ),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7.47", "7.47"),
        ("5.6", "5.60"),
        ("13.5", "13.50"),
        ("12", "12.00"),
        ("7.475", "7.48"),
    ],
)
def test_projection_format_is_always_two_decimal_places(value: str, expected: str) -> None:
    assert format_projection(Decimal(value)) == expected


def test_exact_canonical_gw_tweet_and_home_away_fixture_case() -> None:
    top_three = (
        selection(1, "Haaland", "7.47", ("BUR", True)),
        selection(2, "Salah", "6.2", ("FUL", False)),
        selection(3, "Saka", "6", ("CHE", True)),
    )
    differential = selection(4, "Wood", "5.6", ("TOT", False))

    rendered = render_captain_tweet("GW4", top_three, differential)

    assert rendered == (
        "🧢 CAPTAIN PICKS 🧢\n\n"
        "#GW4 Projected Points:\n\n"
        "🥇 Haaland v BUR (H) - 7.47\n"
        "🥈 Salah v ful (a) - 6.20\n"
        "🥉 Saka v CHE (H) - 6.00\n\n"
        "Differential:\n\n"
        "🐴 Wood v tot (a) - 5.60\n\n"
        "Good luck!\n\n"
        "#FPL #FPLCommunity"
    )
    assert x_weighted_text_length(rendered) <= X_POST_WEIGHTED_LIMIT


def test_representative_dgw_tweet_keeps_all_fixtures_and_fits() -> None:
    top_three = (
        selection(1, "Haaland", "13.5", ("FUL", False), ("TOT", False)),
        selection(2, "Salah", "12", ("BUR", True), ("MCI", True)),
        selection(3, "Saka", "10.4", ("NEW", True), ("AVL", False)),
    )
    differential = selection(4, "Wood", "8.25", ("BHA", True), ("WHU", False))

    rendered = render_captain_tweet("DGW37", top_three, differential)

    assert "🥇 Haaland v ful (a) & tot (a) - 13.50" in rendered
    assert "🥈 Salah v BUR (H) & MCI (H) - 12.00" in rendered
    assert x_weighted_text_length(rendered) <= X_POST_WEIGHTED_LIMIT


@pytest.mark.parametrize("event_code", ["GW4", "BGW29", "DGW37", "BDGW36"])
def test_renderer_reuses_every_supported_event_code(event_code: str) -> None:
    top_three = (
        selection(1, "One", "8", ("AAA", True)),
        selection(2, "Two", "7", ("BBB", True)),
        selection(3, "Three", "6", ("CCC", True)),
    )

    assert f"#{event_code} Projected Points:" in render_captain_tweet(
        event_code,
        top_three,
        selection(4, "Four", "5", ("DDD", True)),
    )


def test_over_limit_tweet_fails_without_dropping_content() -> None:
    top_three = (
        selection(1, "A" * 180, "8", ("AAA", True)),
        selection(2, "Two", "7", ("BBB", True)),
        selection(3, "Three", "6", ("CCC", True)),
    )

    with pytest.raises(CaptainRenderingError, match="exceeds"):
        render_captain_tweet(
            "GW4",
            top_three,
            selection(4, "Four", "5", ("DDD", True)),
        )


def test_weighted_length_normalizes_equivalent_unicode_names() -> None:
    composed = x_weighted_text_length("café")
    decomposed = x_weighted_text_length("cafe\N{COMBINING ACUTE ACCENT}")

    assert composed == decomposed
