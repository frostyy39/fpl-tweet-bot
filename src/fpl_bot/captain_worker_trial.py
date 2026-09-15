"""One-shot, no-post feasibility runner; writes an explicit UTF-8 public audit."""

import argparse
import getpass
import json
from datetime import UTC, datetime
from pathlib import Path

from fpl_bot.api import FplApiClient
from fpl_bot.captain_dry_run import build_captain_report_from_context, fetch_live_captain_context
from fpl_bot.captain_dry_run_cli import render_review_identity_diagnostics
from fpl_bot.captain_result_directory import create_result_directory
from fpl_bot.captain_review_browser import (
    FplReviewBrowserProjectionSource,
    PlaywrightReviewBrowserAcquirer,
)
from fpl_bot.captain_tweet import format_projection, render_fixture, x_weighted_text_length
from fpl_bot.errors import CaptainReviewBrowserError, FplBotError


def selection_audit(report, source):
    """Map resolved records back to original row ordinals, without re-resolving names."""
    projections = source.last_projections
    canonical = source.last_canonical_table
    if projections is None or canonical is None or len(projections) != len(canonical.rows):
        raise CaptainReviewBrowserError("invalid_projection_table")
    ordinals = {
        projection.element_id: row.source_row_ordinal
        for projection, row in zip(projections, canonical.rows, strict=True)
    }
    ranks = {
        projection.element_id: rank
        for rank, projection in enumerate(
            sorted(projections, key=lambda item: item.projected_points, reverse=True), 1
        )
    }

    def selected(item):
        return {
            "element_id": item.player.element_id,
            "web_name": item.player.web_name,
            "source_row_ordinal": ordinals[item.player.element_id],
            "projection_rank": ranks[item.player.element_id],
            "projection": format_projection(item.projected_points),
            "official_ownership": str(item.player.selected_by_percent),
            "fixtures": " & ".join(render_fixture(fixture) for fixture in item.fixtures),
        }

    return {
        "top_three": [selected(item) for item in report.top_three],
        "differential": selected(report.differential),
        "differential_candidates_examined": ranks[report.differential.player.element_id] - 3,
        "tweet": report.tweet,
        "weighted_length": x_weighted_text_length(report.tweet),
    }


def run_trial(profile, output, expected_user):
    """An existing output directory is a durable no-retry guard, even after a crash."""
    profile = profile.resolve()
    output = output.resolve()
    if output == profile or profile in output.parents or output in profile.parents:
        return 2
    if getpass.getuser().casefold() != expected_user.casefold():
        return 2
    try:
        create_result_directory(output)
    except OSError:
        return 2
    audit = {
        "schema_version": 1,
        "execution_user": getpass.getuser(),
        "identity_evidence": "process_username; verify against Windows task/event logs",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "status": "failed",
        "no_post": True,
    }
    acquirer = source = context = None
    exit_code = 1
    try:
        context = fetch_live_captain_context(FplApiClient())
        event = context.event_report
        audit.update(
            event_id=event.event.event_id,
            event_code=event.event_code,
            official_deadline_utc=event.event.deadline_utc.isoformat(),
        )
        acquirer = PlaywrightReviewBrowserAcquirer(profile)
        source = FplReviewBrowserProjectionSource(acquirer, context.players, context.teams)
        report = build_captain_report_from_context(context, source)
        audit.update(selection_audit(report, source))
        audit.update(captain_target_utc=report.captain_target_utc.isoformat(), status="succeeded")
        exit_code = 0
    except CaptainReviewBrowserError as error:
        audit["error_category"] = error.category
        if source is not None and source.last_identity_diagnostic is not None:
            audit["identity_failures"] = json.loads(
                render_review_identity_diagnostics(
                    source.last_identity_diagnostic,
                    context.event_report,
                    source.last_canonical_table,
                )
            )
    except FplBotError as error:
        audit["error_category"] = type(error).__name__
    except Exception:
        # Never persist arbitrary exception messages, browser stderr, or response bodies.
        audit["error_category"] = "unexpected_worker_failure"
    if acquirer is not None:
        audit["browser_lifecycle"] = acquirer.last_lifecycle
    if source is not None and source.last_acquisition is not None:
        acquisition = source.last_acquisition
        audit.update(
            acquired_at_utc=acquisition.acquired_at_utc.isoformat(),
            acquisition_time_is_dataset_generation_time=False,
            extracted_rows=acquisition.total_projection_rows,
            resolved_rows=acquisition.resolved_candidate_count,
            identity_failure_count=acquisition.total_projection_rows
            - acquisition.resolved_candidate_count,
        )
    if source is not None and source.last_canonical_table is not None:
        table = source.last_canonical_table
        audit.update(
            raw_rows=table.raw_body_row_count,
            genuine_rows=table.genuine_player_row_count,
            auxiliary_rows=table.auxiliary_row_count,
        )
    audit.update(ended_at_utc=datetime.now(UTC).isoformat(), exit_code=exit_code)
    try:
        with (output / "audit.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(audit, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except OSError:
        return 2
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expected-user", required=True)
    args = parser.parse_args(argv)
    return run_trial(args.profile_dir, args.output_dir, args.expected_user)


if __name__ == "__main__":
    raise SystemExit(main())
