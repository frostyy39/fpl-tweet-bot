"""Pure Captain release policy. No clock reads, scheduling or provider dependencies."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fpl_bot.captain_timing import captain_target_utc

CAPTAIN_LATENESS = timedelta(minutes=5)
CAPTAIN_WARMUP_LEAD = timedelta(minutes=15)
LONDON = ZoneInfo("Europe/London")


def require_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be aware UTC")


@dataclass(frozen=True, slots=True)
class CaptainTiming:
    deadline_utc: datetime

    def __post_init__(self) -> None:
        require_utc(self.deadline_utc)
        # Validate representability of every derived boundary now, not at dispatch.
        try:
            for value in (self.deadline_utc, self.target_utc, self.warmup_utc, self.expiry_utc):
                value.astimezone(LONDON)
        except (OverflowError, ValueError):
            raise ValueError("unrepresentable Captain timing") from None

    @property
    def target_utc(self) -> datetime:
        return captain_target_utc(self.deadline_utc)

    @property
    def release_utc(self) -> datetime:
        return self.target_utc

    @property
    def warmup_utc(self) -> datetime:
        return self.target_utc - CAPTAIN_WARMUP_LEAD

    @property
    def expiry_utc(self) -> datetime:
        return self.target_utc + CAPTAIN_LATENESS

    @property
    def target_london(self) -> datetime:
        return self.target_utc.astimezone(LONDON)

    @property
    def warmup_london(self) -> datetime:
        return self.warmup_utc.astimezone(LONDON)

    def permits_new_attempt(self, now_utc: datetime) -> bool:
        """Inclusive [T,L]; only a new attempt's initiation time is governed here."""
        require_utc(now_utc)
        return self.release_utc <= now_utc <= self.expiry_utc

    def permits_posting_acquisition(self, started_at_utc: datetime) -> bool:
        return self.permits_new_attempt(started_at_utc)

    def permits_warmup(self, now_utc: datetime) -> bool:
        require_utc(now_utc)
        return self.warmup_utc <= now_utc < self.release_utc


def utc_text(value: datetime) -> str:
    require_utc(value)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
