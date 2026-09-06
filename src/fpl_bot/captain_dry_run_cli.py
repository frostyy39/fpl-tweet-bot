"""Print one real-data Captain dry run locally; this module cannot post."""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from fpl_bot.api import FplApiClient
from fpl_bot.captain_dry_run import (
    build_captain_report_from_context,
    fetch_live_captain_context,
    render_captain_audit,
)
from fpl_bot.captain_projection import FplReviewCsvProjectionSource
from fpl_bot.captain_review_browser import (
    FplReviewBrowserProjectionSource,
    PlaywrightReviewBrowserAcquirer,
    ReviewCanonicalProjectionTable,
    ReviewHeaderSignature,
    ReviewIdentityDiagnosticSummary,
    ReviewNavigationDiagnostic,
    ReviewRowExtractionDiagnostic,
    ReviewStructuralCell,
    ReviewTableStructuralInspection,
)
from fpl_bot.errors import CaptainReviewBrowserError, FplBotError
from fpl_bot.models import EventReport


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_output()
    parser = argparse.ArgumentParser(
        description="Render Captain picks locally from live FPL data and FPL Review projections."
    )
    parser.add_argument(
        "--source",
        choices=("review-csv", "review-browser"),
        default="review-csv",
        help="Projection source (default: review-csv)",
    )
    parser.add_argument(
        "--projections-csv",
        type=Path,
        help="Path to a manually exported FPL Review projections CSV",
    )
    parser.add_argument(
        "--review-profile-dir",
        type=Path,
        help="Dedicated authenticated FPL Review browser profile outside this repository",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="FPL HTTP timeout in seconds (default: 10)",
    )
    args = parser.parse_args(argv)

    if args.source == "review-csv" and args.projections_csv is None:
        parser.error("--projections-csv is required for --source review-csv")
    if args.source == "review-browser" and args.review_profile_dir is None:
        parser.error("--review-profile-dir is required for --source review-browser")
    if args.source == "review-browser" and args.projections_csv is not None:
        parser.error("--projections-csv cannot be used with --source review-browser")

    context = None
    browser_source = None
    browser_acquirer = None
    try:
        context = fetch_live_captain_context(FplApiClient(timeout_seconds=args.timeout))
        if args.source == "review-browser":
            browser_acquirer = PlaywrightReviewBrowserAcquirer(
                args.review_profile_dir,
                timeout_seconds=args.timeout,
            )
            browser_source = FplReviewBrowserProjectionSource(
                browser_acquirer,
                context.players,
                context.teams,
            )
            projection_source = browser_source
        else:
            browser_source = None
            projection_source = FplReviewCsvProjectionSource(args.projections_csv)
        report = build_captain_report_from_context(context, projection_source)
    except FplBotError as exc:
        if isinstance(exc, CaptainReviewBrowserError):
            print(f"Captain dry run failed: {exc.category}", file=sys.stderr)
            if (
                context is not None
                and browser_source is not None
                and browser_source.last_identity_diagnostic is not None
            ):
                print(
                    render_review_identity_diagnostics(
                        browser_source.last_identity_diagnostic,
                        context.event_report,
                        browser_source.last_canonical_table,
                    )
                )
            elif (
                browser_source is not None
                and browser_source.last_row_extraction_diagnostic is not None
            ):
                print(
                    render_review_row_extraction_diagnostic(
                        browser_source.last_row_extraction_diagnostic
                    )
                )
            elif browser_source is not None and browser_source.last_structural_inspections:
                print(
                    render_review_structural_inspections(
                        browser_source.last_structural_inspections,
                        exc.category,
                    )
                )
            if (
                browser_acquirer is not None
                and browser_acquirer.last_navigation_diagnostic is not None
            ):
                print(
                    render_review_navigation_diagnostic(browser_acquirer.last_navigation_diagnostic)
                )
        else:
            print(f"Captain dry run failed: {exc}", file=sys.stderr)
        return 1

    print(render_captain_audit(report))
    if browser_source is not None:
        acquisition = browser_source.last_acquisition
        projections = browser_source.last_projections
        if acquisition is None or projections is None:
            print("Captain dry run failed: invalid_projection_table", file=sys.stderr)
            return 1
        ranked = sorted(projections, key=lambda item: item.projected_points, reverse=True)
        differential_rank = next(
            index
            for index, item in enumerate(ranked, start=1)
            if item.element_id == report.differential.player.element_id
        )
        print(
            "\nFPL Review browser acquisition:\n"
            f"Acquired UTC: {acquisition.acquired_at_utc.isoformat(timespec='seconds')} "
            "(local acquisition time; not Review dataset-generation time)\n"
            f"Projection rows extracted: {acquisition.total_projection_rows}\n"
            f"Official FPL candidates resolved: {acquisition.resolved_candidate_count}\n"
            f"Raw body rows observed: {browser_source.last_canonical_table.raw_body_row_count}\n"
            f"Genuine player rows: {browser_source.last_canonical_table.genuine_player_row_count}\n"
            f"Auxiliary rows: {browser_source.last_canonical_table.auxiliary_row_count} "
            f"({browser_source.last_canonical_table.auxiliary_reason})\n"
            f"Differential full-table rank: {differential_rank}"
        )
        if browser_acquirer is not None and browser_acquirer.last_navigation_diagnostic is not None:
            print(render_review_navigation_diagnostic(browser_acquirer.last_navigation_diagnostic))
    return 0


def render_review_identity_diagnostics(
    summary: ReviewIdentityDiagnosticSummary,
    event_report: EventReport,
    canonical: ReviewCanonicalProjectionTable | None = None,
) -> str:
    """Render only explicitly allowlisted public FPL/Review identity metadata."""
    payload = {
        "acquired_at_utc": summary.acquired_at_utc.isoformat(timespec="seconds"),
        "event_code": event_report.event_code,
        "event_id": summary.event_id,
        "failure_count": len(summary.failures),
        "failures": [
            {
                "category": failure.category,
                "exact_name_exists": failure.exact_name_exists,
                "exact_name_official_matches": [
                    {
                        "element_id": match.element_id,
                        "team_short_code": match.team_short_code,
                        "web_name": match.web_name,
                    }
                    for match in failure.exact_name_official_matches
                ],
                "exact_official_match_count": failure.exact_official_match_count,
                "normalized_review_name": failure.normalized_review_name,
                "normalized_review_team_code": failure.normalized_review_team_code,
                "review_display_name": failure.review_display_name,
                "review_team_code_exists": failure.review_team_code_exists,
                "review_team_short_code": failure.review_team_short_code,
                "same_team_official_players": [
                    {
                        "element_id": match.element_id,
                        "team_short_code": match.team_short_code,
                        "web_name": match.web_name,
                    }
                    for match in failure.same_team_official_players
                ],
                "source_row_ordinal": failure.source_row_ordinal,
            }
            for failure in summary.failures
        ],
        "matching_review_column": summary.matched_gameweek_column,
        "official_deadline_utc": event_report.event.deadline_utc.isoformat(timespec="seconds"),
        "resolved_candidate_count": summary.resolved_candidate_count,
        "result": "identity_resolution_failed",
        "total_projection_rows": summary.total_projection_rows,
    }
    if canonical is not None:
        payload["logical_table"] = {
            "logical_header_count": len(canonical.logical_headers),
            "logical_header_raw_indexes": canonical.logical_header_raw_indexes,
            "logical_headers": canonical.logical_headers,
            "logical_row_cell_count": canonical.logical_row_cell_count,
            "raw_header_count": canonical.raw_header_count,
            "representative_raw_row_cell_count": (canonical.representative_raw_row_cell_count),
            "raw_body_row_count": canonical.raw_body_row_count,
            "genuine_player_row_count": canonical.genuine_player_row_count,
            "auxiliary_row_count": canonical.auxiliary_row_count,
            "auxiliary_reason": canonical.auxiliary_reason,
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def render_review_row_extraction_diagnostic(
    diagnostic: ReviewRowExtractionDiagnostic,
) -> str:
    """Render only bounded public cells needed to diagnose minimal row extraction."""
    payload = {
        "auxiliary_reason": diagnostic.auxiliary_reason,
        "auxiliary_row_count": diagnostic.auxiliary_row_count,
        "failures": [
            {
                "player_cell_text": failure.player_cell_text,
                "reason": failure.reason,
                "selected_gameweek_cell_text": failure.selected_gameweek_cell_text,
                "source_row_ordinal": failure.source_row_ordinal,
            }
            for failure in diagnostic.failures
        ],
        "genuine_player_row_count": diagnostic.genuine_player_row_count,
        "raw_body_row_count": diagnostic.raw_body_row_count,
        "result": "projection_row_extraction_failed",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def render_review_structural_inspections(
    inspections: Sequence[ReviewTableStructuralInspection],
    category: str,
) -> str:
    """Render only bounded public DOM structure from tables containing the selected GW."""
    payload = {
        "category": category,
        "result": "projection_table_structure",
        "tables": [
            {
                "header_row_index": inspection.header_row_index,
                "headers": [_render_structural_cell(cell) for cell in inspection.headers],
                "sample_rows": [
                    {
                        "cell_count": row.cell_count,
                        "cells": [_render_structural_cell(cell) for cell in row.cells],
                        "source_row_ordinal": row.source_row_ordinal,
                    }
                    for row in inspection.sample_rows
                ],
                "table_index": inspection.table_index,
                "total_header_count": inspection.total_header_count,
            }
            for inspection in inspections
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def render_review_navigation_diagnostic(diagnostic: ReviewNavigationDiagnostic) -> str:
    """Render bounded public navigation timing and table-header observations."""
    payload = {
        "after_click_at_utc": _optional_timestamp(diagnostic.after_click_at_utc),
        "all_required_same_table_at_utc": _optional_timestamp(
            diagnostic.all_required_same_table_at_utc
        ),
        "app_loaded_at_utc": _optional_timestamp(diagnostic.app_loaded_at_utc),
        "before_click_at_utc": _optional_timestamp(diagnostic.before_click_at_utc),
        "click_completed": diagnostic.click_completed,
        "final_header_signatures": [
            _render_header_signature(signature) for signature in diagnostic.final_header_signatures
        ],
        "final_public_url": diagnostic.final_public_url,
        "first_elite_ownership_at_utc": _optional_timestamp(
            diagnostic.first_elite_ownership_at_utc
        ),
        "first_expected_gameweek_at_utc": _optional_timestamp(
            diagnostic.first_expected_gameweek_at_utc
        ),
        "first_player_at_utc": _optional_timestamp(diagnostic.first_player_at_utc),
        "first_price_at_utc": _optional_timestamp(diagnostic.first_price_at_utc),
        "first_total_at_utc": _optional_timestamp(diagnostic.first_total_at_utc),
        "initial_public_url": diagnostic.initial_public_url,
        "initial_semantic_table_present": diagnostic.initial_semantic_table_present,
        "observations": [
            {
                "elapsed_milliseconds": observation.elapsed_milliseconds,
                "elite_ownership_present": observation.elite_ownership_present,
                "expected_gameweek_present": observation.expected_gameweek_present,
                "header_signatures": [
                    _render_header_signature(signature)
                    for signature in observation.header_signatures
                ],
                "observed_at_utc": _optional_timestamp(observation.observed_at_utc),
                "player_present": observation.player_present,
                "price_present": observation.price_present,
                "relevant_control_texts": observation.relevant_control_texts,
                "semantic_block_count": observation.semantic_block_count,
                "total_present": observation.total_present,
                "visible_projections_control_count": (
                    observation.visible_projections_control_count
                ),
            }
            for observation in diagnostic.observations
        ],
        "ready_at_utc": _optional_timestamp(diagnostic.ready_at_utc),
        "result": "projections_navigation_diagnostic",
        "visible_projections_control_count": (diagnostic.visible_projections_control_count),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _render_header_signature(signature: ReviewHeaderSignature) -> dict[str, object]:
    return {
        "header_row_index": signature.header_row_index,
        "headers": signature.headers,
        "table_index": signature.table_index,
    }


def _optional_timestamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value is not None else None


def _render_structural_cell(cell: ReviewStructuralCell) -> dict[str, object]:
    return {
        "aria_hidden": cell.aria_hidden,
        "class": cell.class_name,
        "display": cell.display,
        "hidden": cell.hidden,
        "index": cell.index,
        "tag": cell.tag_name,
        "text": cell.text,
        "visibility": cell.visibility,
    }


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _configure_utf8_output() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
