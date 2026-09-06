"""Local-only Captain orchestration over live FPL data and a projection source."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fpl_bot.captain_models import CaptainReport, CaptainSelection
from fpl_bot.captain_projection import CaptainProjectionSource
from fpl_bot.captain_service import build_captain_report
from fpl_bot.captain_tweet import format_projection, render_fixture, x_weighted_text_length
from fpl_bot.errors import DataValidationError
from fpl_bot.events import parse_events, select_next_event
from fpl_bot.models import EventReport, Fixture, FplPlayer, Team
from fpl_bot.parsing import parse_fixtures, parse_players, parse_teams
from fpl_bot.service import FplDataSource, build_event_report


@dataclass(frozen=True, slots=True)
class LiveCaptainContext:
    event_report: EventReport
    players: tuple[FplPlayer, ...]
    teams: tuple[Team, ...]
    fixtures: tuple[Fixture, ...]


def build_live_captain_report(
    fpl_source: FplDataSource,
    projection_source: CaptainProjectionSource,
    *,
    now: datetime | None = None,
) -> CaptainReport:
    context = fetch_live_captain_context(fpl_source, now=now)
    return build_captain_report_from_context(context, projection_source)


def fetch_live_captain_context(
    fpl_source: FplDataSource,
    *,
    now: datetime | None = None,
) -> LiveCaptainContext:
    bootstrap = fpl_source.fetch_bootstrap_static()
    _require_bootstrap_fields(bootstrap)
    event = select_next_event(parse_events(bootstrap["events"]), now=now)
    teams = parse_teams(bootstrap["teams"])
    players = parse_players(bootstrap["elements"])
    fixtures = parse_fixtures(
        list(fpl_source.fetch_event_fixtures(event.event_id)),
        expected_event_id=event.event_id,
    )
    event_report = build_event_report(event, teams, fixtures)
    return LiveCaptainContext(event_report, players, teams, fixtures)


def build_captain_report_from_context(
    context: LiveCaptainContext,
    projection_source: CaptainProjectionSource,
) -> CaptainReport:
    return build_captain_report(
        context.event_report,
        projection_source,
        context.players,
        context.teams,
        context.fixtures,
    )


def render_captain_audit(report: CaptainReport) -> str:
    lines = [
        "Captain dry run (local/read-only)",
        f"Event ID: {report.event.event_id}",
        f"Event code: {report.event_code}",
        f"Official deadline UTC: {report.event.deadline_utc.isoformat(timespec='seconds')}",
        f"Captain target UTC: {report.captain_target_utc.isoformat(timespec='seconds')}",
        "Captain target Europe/London: "
        f"{report.captain_target_london.isoformat(timespec='seconds')} "
        f"({report.captain_target_london.tzname()})",
        "Top three:",
    ]
    lines.extend(
        _audit_selection(index, selection)
        for index, selection in enumerate(report.top_three, start=1)
    )
    lines.extend(
        (
            "Differential:",
            _audit_selection(None, report.differential, include_ownership=True),
            f"Final weighted character count: {x_weighted_text_length(report.tweet)}",
            "Rendered tweet:",
            report.tweet,
        )
    )
    return "\n".join(lines)


def _require_bootstrap_fields(bootstrap: Mapping[str, Any]) -> None:
    required = ("events", "teams", "elements")
    if any(field not in bootstrap for field in required):
        raise DataValidationError("FPL bootstrap response must contain events, teams, and elements")


def _audit_selection(
    rank: int | None,
    selection: CaptainSelection,
    *,
    include_ownership: bool = False,
) -> str:
    label = f"  {rank}." if rank is not None else "  Pick:"
    fixtures = " & ".join(render_fixture(fixture) for fixture in selection.fixtures)
    ownership = (
        f"; official ownership={selection.player.selected_by_percent}%" if include_ownership else ""
    )
    return (
        f"{label} ID={selection.player.element_id}; name={selection.player.web_name}; "
        f"projection={format_projection(selection.projected_points)}; "
        f"fixtures={fixtures}{ownership}"
    )
