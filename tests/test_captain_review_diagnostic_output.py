import json
from datetime import UTC, datetime
from decimal import Decimal

from fpl_bot.captain_dry_run_cli import (
    render_review_identity_diagnostics,
    render_review_navigation_diagnostic,
    render_review_row_extraction_diagnostic,
    render_review_structural_inspections,
)
from fpl_bot.captain_review_browser import (
    ReviewCanonicalProjectionTable,
    ReviewIdentityDiagnostic,
    ReviewIdentityDiagnosticSummary,
    ReviewLogicalProjectionRow,
    ReviewNavigationDiagnostic,
    ReviewOfficialIdentity,
    ReviewReadinessObservation,
    ReviewRowExtractionDiagnostic,
    ReviewRowExtractionFailure,
    ReviewStructuralCell,
    ReviewStructuralRow,
    ReviewTableStructuralInspection,
)
from fpl_bot.models import EventReport, FixtureClassification, FplEvent


def test_identity_diagnostic_output_has_only_allowlisted_public_fields() -> None:
    official = ReviewOfficialIdentity("Official Name", "ARS", 123)
    summary = ReviewIdentityDiagnosticSummary(
        event_id=4,
        matched_gameweek_column="GW4",
        acquired_at_utc=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
        total_projection_rows=50,
        resolved_candidate_count=49,
        failures=(
            ReviewIdentityDiagnostic(
                source_row_ordinal=26,
                review_display_name="Review Name",
                review_team_short_code="ARS",
                normalized_review_name="Review Name",
                normalized_review_team_code="ARS",
                exact_official_match_count=0,
                exact_name_exists=False,
                exact_name_official_matches=(),
                review_team_code_exists=True,
                same_team_official_players=(official,),
                category="zero_exact_match",
            ),
        ),
    )
    report = EventReport(
        event=FplEvent(
            4,
            "Gameweek 4",
            datetime(2026, 9, 12, 17, 30, tzinfo=UTC),
            False,
            True,
        ),
        deadline_london=datetime(2026, 9, 12, 18, 30, tzinfo=UTC),
        classification=FixtureClassification(kind="GW", team_counts=()),
        event_code="GW4",
        tweet="unused",
    )

    canonical = ReviewCanonicalProjectionTable(
        event_id=4,
        matched_gameweek_column="GW4",
        raw_header_count=6,
        logical_headers=("PLAYER", "PRICE", "GW4", "TOTAL", "ELITE OWN%"),
        logical_header_raw_indexes=(0, 1, 2, 3, 4),
        representative_raw_row_cell_count=6,
        logical_row_cell_count=5,
        raw_body_row_count=1,
        genuine_player_row_count=1,
        auxiliary_row_count=0,
        auxiliary_reason="no_visible_logical_column_block",
        rows=(ReviewLogicalProjectionRow(1, "Official Name", "ARS", Decimal("7.5")),),
    )
    payload = json.loads(render_review_identity_diagnostics(summary, report, canonical))

    assert set(payload) == {
        "acquired_at_utc",
        "event_code",
        "event_id",
        "failure_count",
        "failures",
        "matching_review_column",
        "logical_table",
        "official_deadline_utc",
        "resolved_candidate_count",
        "result",
        "total_projection_rows",
    }
    assert set(payload["failures"][0]) == {
        "category",
        "exact_name_exists",
        "exact_name_official_matches",
        "exact_official_match_count",
        "normalized_review_name",
        "normalized_review_team_code",
        "review_display_name",
        "review_team_code_exists",
        "review_team_short_code",
        "same_team_official_players",
        "source_row_ordinal",
    }
    rendered = json.dumps(payload)
    for forbidden in ("cookie", "session", "token", "authorization", "profile"):
        assert forbidden not in rendered.lower()


def test_row_extraction_diagnostic_has_only_bounded_public_fields() -> None:
    diagnostic = ReviewRowExtractionDiagnostic(
        raw_body_row_count=3,
        genuine_player_row_count=2,
        auxiliary_row_count=1,
        auxiliary_reason="no_visible_logical_column_block",
        failures=(
            ReviewRowExtractionFailure(
                source_row_ordinal=2,
                player_cell_text="Malformed player",
                selected_gameweek_cell_text="not-points",
                reason="invalid_selected_gameweek_projection",
            ),
        ),
    )

    rendered = render_review_row_extraction_diagnostic(diagnostic)
    payload = json.loads(rendered)

    assert payload == {
        "auxiliary_reason": "no_visible_logical_column_block",
        "auxiliary_row_count": 1,
        "failures": [
            {
                "player_cell_text": "Malformed player",
                "reason": "invalid_selected_gameweek_projection",
                "selected_gameweek_cell_text": "not-points",
                "source_row_ordinal": 2,
            }
        ],
        "genuine_player_row_count": 2,
        "raw_body_row_count": 3,
        "result": "projection_row_extraction_failed",
    }
    for forbidden in ("cookie", "session", "token", "authorization", "profile"):
        assert forbidden not in rendered.lower()


def test_structural_diagnostic_output_contains_only_allowlisted_dom_metadata() -> None:
    cell = ReviewStructuralCell(
        index=0,
        text="PLAYER",
        tag_name="th",
        class_name="desktop",
        aria_hidden=None,
        hidden=False,
        display="table-cell",
        visibility="visible",
    )
    inspection = ReviewTableStructuralInspection(
        table_index=0,
        header_row_index=0,
        total_header_count=1,
        headers=(cell,),
        sample_rows=(ReviewStructuralRow(1, 1, (cell,)),),
    )

    payload = json.loads(
        render_review_structural_inspections((inspection,), "invalid_projection_table")
    )

    assert set(payload) == {"category", "result", "tables"}
    assert set(payload["tables"][0]) == {
        "header_row_index",
        "headers",
        "sample_rows",
        "table_index",
        "total_header_count",
    }
    assert set(payload["tables"][0]["headers"][0]) == {
        "aria_hidden",
        "class",
        "display",
        "hidden",
        "index",
        "tag",
        "text",
        "visibility",
    }
    rendered = json.dumps(payload)
    for forbidden in ("cookie", "session", "token", "authorization", "profile"):
        assert forbidden not in rendered.lower()


def test_navigation_diagnostic_output_is_bounded_to_public_dom_state() -> None:
    observed = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    observation = ReviewReadinessObservation(
        observed_at_utc=observed,
        elapsed_milliseconds=200,
        visible_projections_control_count=1,
        semantic_block_count=1,
        player_present=True,
        price_present=True,
        expected_gameweek_present=True,
        total_present=True,
        elite_ownership_present=True,
        header_signatures=(),
        relevant_control_texts=("PROJECTIONS",),
    )
    diagnostic = ReviewNavigationDiagnostic(
        app_loaded_at_utc=observed,
        initial_public_url="https://app.fplreview.com/",
        final_public_url="https://app.fplreview.com/",
        initial_semantic_table_present=False,
        visible_projections_control_count=1,
        before_click_at_utc=observed,
        click_completed=True,
        after_click_at_utc=observed,
        first_player_at_utc=observed,
        first_price_at_utc=observed,
        first_expected_gameweek_at_utc=observed,
        first_total_at_utc=observed,
        first_elite_ownership_at_utc=observed,
        all_required_same_table_at_utc=observed,
        ready_at_utc=observed,
        observations=(observation,),
        final_header_signatures=(),
    )

    rendered = render_review_navigation_diagnostic(diagnostic)
    payload = json.loads(rendered)

    assert payload["result"] == "projections_navigation_diagnostic"
    assert payload["click_completed"] is True
    assert payload["observations"][0]["semantic_block_count"] == 1
    assert payload["observations"][0]["relevant_control_texts"] == ["PROJECTIONS"]
    for forbidden in ("cookie", "session", "token", "authorization", "profile"):
        assert forbidden not in rendered.lower()
