from decimal import Decimal

import pytest

from fpl_bot.captain_projection import FplReviewCsvProjectionSource
from fpl_bot.errors import CaptainProjectionError


def write_csv(tmp_path, content: str):
    path = tmp_path / "review.csv"
    path.write_text(content, encoding="utf-8")
    return path


def test_reads_selected_event_column_with_exact_decimals_and_source_order(tmp_path) -> None:
    path = write_csv(
        tmp_path,
        "ID,Name,3_Pts,4_Pts\n2,Second,8.00,7.50\n1,First,9.00,7.50\n3,Third,6,6.125\n",
    )

    projections = FplReviewCsvProjectionSource(path).fetch_event_projections(4)

    assert [(item.element_id, item.projected_points) for item in projections] == [
        (2, Decimal("7.50")),
        (1, Decimal("7.50")),
        (3, Decimal("6.125")),
    ]


def test_rejects_missing_event_projection_column(tmp_path) -> None:
    path = write_csv(tmp_path, "ID,3_Pts\n1,7.5\n")

    with pytest.raises(CaptainProjectionError, match="no projection column for event 4"):
        FplReviewCsvProjectionSource(path).fetch_event_projections(4)


@pytest.mark.parametrize("points", ["", "not-a-number", "NaN", "Infinity"])
def test_rejects_malformed_projection(tmp_path, points: str) -> None:
    path = write_csv(tmp_path, f"ID,4_Pts\n1,{points}\n")

    with pytest.raises(CaptainProjectionError, match="invalid projected points"):
        FplReviewCsvProjectionSource(path).fetch_event_projections(4)


def test_rejects_duplicate_player_id(tmp_path) -> None:
    path = write_csv(tmp_path, "ID,4_Pts\n1,7.5\n1,6.5\n")

    with pytest.raises(CaptainProjectionError, match="duplicate player ID 1"):
        FplReviewCsvProjectionSource(path).fetch_event_projections(4)


@pytest.mark.parametrize("element_id", ["", "0", "-1", "1.0", "01", "player"])
def test_rejects_noncanonical_player_id(tmp_path, element_id: str) -> None:
    path = write_csv(tmp_path, f"ID,4_Pts\n{element_id},7.5\n")

    with pytest.raises(CaptainProjectionError, match="invalid player ID"):
        FplReviewCsvProjectionSource(path).fetch_event_projections(4)
