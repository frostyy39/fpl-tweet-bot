"""Safe local dry-run command. This module has no posting capability."""

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime

from fpl_bot.api import FplApiClient
from fpl_bot.errors import FplBotError
from fpl_bot.models import EventReport, TeamFixtureCount
from fpl_bot.service import build_next_event_report


def render_report(report: EventReport) -> str:
    lines = [
        "FPL Bot Milestone 1 dry run (read-only)",
        f"Event ID: {report.event.event_id}",
        f"Official deadline UTC: {_format_datetime(report.event.deadline_utc)}",
        "Deadline Europe/London: "
        f"{_format_datetime(report.deadline_london)} ({report.deadline_london.tzname()})",
        f"Classification: {report.classification.kind.value}",
        f"Event code: {report.event_code}",
        f"Blank teams: {_format_diagnostic_teams(report.classification.blank_teams)}",
        f"Double teams: {_format_diagnostic_teams(report.classification.double_teams)}",
        "Fixture counts:",
    ]
    lines.extend(
        f"  {item.team.short_name} ({item.team.name}): {item.fixture_count}"
        for item in report.classification.team_counts
    )
    lines.extend(("Rendered tweet:", report.tweet))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_output()
    parser = argparse.ArgumentParser(
        description="Query live FPL data and print the next deadline tweet without posting it."
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds (default: 10)",
    )
    args = parser.parse_args(argv)

    try:
        report = build_next_event_report(FplApiClient(timeout_seconds=args.timeout))
    except FplBotError as exc:
        print(f"Dry run failed: {exc}", file=sys.stderr)
        return 1

    print(render_report(report))
    return 0


def _format_datetime(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _format_diagnostic_teams(teams: tuple[TeamFixtureCount, ...]) -> str:
    if not teams:
        return "None"
    return ", ".join(
        f"{item.team.short_name} ({item.fixture_count})"
        if item.fixture_count > 1
        else item.team.short_name
        for item in teams
    )


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
