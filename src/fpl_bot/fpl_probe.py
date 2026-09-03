"""One-shot FPL connectivity/planning probe with no task or posting capabilities."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fpl_bot.api import FplApiClient
from fpl_bot.deadline_planning import DeadlinePlanner
from fpl_bot.models import FplEvent
from fpl_bot.service import FplDataSource


@dataclass(frozen=True, slots=True)
class FplProbeResult:
    """Public, non-secret metadata from the real read-only planning path."""

    event: FplEvent
    observed_at_utc: datetime
    deadline_london: datetime
    is_current_london_day: bool


Clock = Callable[[], datetime]


class FplReadOnlyProbe:
    """Observe production deadline planning without composing any mutable boundary."""

    def __init__(self, source: FplDataSource, *, clock: Clock | None = None) -> None:
        self._planner = DeadlinePlanner(source, clock=clock)

    def run(self) -> FplProbeResult:
        observation = self._planner.observe()
        return FplProbeResult(
            event=observation.event,
            observed_at_utc=observation.observed_at_utc,
            deadline_london=observation.deadline_london,
            is_current_london_day=observation.is_current_london_day,
        )


def create_fpl_probe(*, clock: Clock | None = None) -> FplReadOnlyProbe:
    """Compose only the public FPL reader and pure deadline planner."""

    return FplReadOnlyProbe(FplApiClient(), clock=clock)
