"""Orchestration of the read-only deterministic FPL analysis."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from fpl_bot.classification import classify_fixtures, render_event_code
from fpl_bot.errors import DataValidationError
from fpl_bot.events import parse_events, select_next_event, to_london
from fpl_bot.models import EventReport
from fpl_bot.parsing import parse_fixtures, parse_teams
from fpl_bot.tweet import render_v1_tweet


class FplDataSource(Protocol):
    def fetch_bootstrap_static(self) -> Mapping[str, Any]: ...

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]: ...


def build_next_event_report(
    source: FplDataSource,
    now: datetime | None = None,
) -> EventReport:
    bootstrap = source.fetch_bootstrap_static()
    if "events" not in bootstrap or "teams" not in bootstrap:
        raise DataValidationError("FPL bootstrap response must contain events and teams")

    events = parse_events(bootstrap["events"])
    teams = parse_teams(bootstrap["teams"])
    event = select_next_event(events, now=now)
    fixture_payload = source.fetch_event_fixtures(event.event_id)
    fixtures = parse_fixtures(list(fixture_payload), expected_event_id=event.event_id)
    classification = classify_fixtures(teams, fixtures)
    event_code = render_event_code(event.event_id, classification.kind)

    return EventReport(
        event=event,
        deadline_london=to_london(event.deadline_utc),
        classification=classification,
        event_code=event_code,
        tweet=render_v1_tweet(event_code),
    )
