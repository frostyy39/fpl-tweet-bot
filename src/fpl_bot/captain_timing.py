"""UTC-first timing for the Captain recommendation post."""

from datetime import UTC, datetime, timedelta

from fpl_bot.events import to_london

CAPTAIN_LEAD_TIME = timedelta(hours=2)


def captain_target_utc(official_deadline_utc: datetime) -> datetime:
    if (
        not isinstance(official_deadline_utc, datetime)
        or official_deadline_utc.tzinfo is None
        or official_deadline_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("official FPL deadline must be timezone-aware UTC")
    return official_deadline_utc.astimezone(UTC) - CAPTAIN_LEAD_TIME


def captain_target_london(official_deadline_utc: datetime) -> datetime:
    return to_london(captain_target_utc(official_deadline_utc))
