"""FPL event parsing, selection, and timezone conversion."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fpl_bot.errors import DataValidationError, NoSuitableEventError
from fpl_bot.models import FplEvent

LONDON_TIMEZONE = ZoneInfo("Europe/London")


def parse_deadline(value: object) -> datetime:
    """Parse an ISO-8601 FPL deadline and normalize it to aware UTC."""
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError("deadline_time must be a non-empty ISO-8601 string")

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        deadline = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DataValidationError(f"Invalid FPL deadline_time: {value!r}") from exc

    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise DataValidationError("deadline_time must include a UTC offset")
    return deadline.astimezone(UTC)


def parse_events(payload: object) -> tuple[FplEvent, ...]:
    if not isinstance(payload, list):
        raise DataValidationError("FPL events must be a JSON array")

    events = tuple(_parse_event(item) for item in payload)
    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise DataValidationError("FPL events contain duplicate event IDs")
    return events


def select_next_event(
    events: Sequence[FplEvent],
    now: datetime | None = None,
) -> FplEvent:
    """Select FPL's explicit next/current future event, then fall back by deadline."""
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    reference_utc = reference.astimezone(UTC)
    eligible = tuple(event for event in events if event.deadline_utc >= reference_utc)
    if not eligible:
        raise NoSuitableEventError("FPL exposes no event with a current or future deadline")

    for attribute in ("is_next", "is_current"):
        preferred = tuple(event for event in eligible if getattr(event, attribute))
        if len(preferred) > 1:
            raise DataValidationError(f"FPL marks multiple eligible events as {attribute}")
        if preferred:
            return preferred[0]

    return min(eligible, key=lambda event: (event.deadline_utc, event.event_id))


def to_london(deadline_utc: datetime) -> datetime:
    if deadline_utc.tzinfo is None or deadline_utc.utcoffset() is None:
        raise ValueError("deadline_utc must be timezone-aware")
    return deadline_utc.astimezone(LONDON_TIMEZONE)


def _parse_event(payload: object) -> FplEvent:
    if not isinstance(payload, Mapping):
        raise DataValidationError("Each FPL event must be a JSON object")

    event_id = _positive_int(payload, "id", "event")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DataValidationError(f"Event {event_id} has no valid name")

    return FplEvent(
        event_id=event_id,
        name=name.strip(),
        deadline_utc=parse_deadline(payload.get("deadline_time")),
        is_current=_boolean(payload, "is_current", event_id),
        is_next=_boolean(payload, "is_next", event_id),
    )


def _positive_int(payload: Mapping[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DataValidationError(f"FPL {label} {key} must be a positive integer")
    return value


def _boolean(payload: Mapping[str, Any], key: str, event_id: int) -> bool:
    value = payload.get(key, False)
    if not isinstance(value, bool):
        raise DataValidationError(f"Event {event_id} field {key} must be boolean")
    return value
