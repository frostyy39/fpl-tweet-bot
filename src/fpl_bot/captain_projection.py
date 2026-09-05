"""Source boundaries for ranked Captain projection data."""

import csv
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from fpl_bot.captain_models import CaptainProjection
from fpl_bot.errors import CaptainProjectionError


class CaptainProjectionSource(Protocol):
    def fetch_event_projections(self, event_id: int) -> Sequence[CaptainProjection]: ...


class InMemoryCaptainProjectionSource:
    """Deterministic projection source for local orchestration and tests."""

    def __init__(self, projections_by_event: Mapping[int, Sequence[CaptainProjection]]) -> None:
        self._projections_by_event = {
            event_id: tuple(projections) for event_id, projections in projections_by_event.items()
        }

    def fetch_event_projections(self, event_id: int) -> tuple[CaptainProjection, ...]:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise CaptainProjectionError("Captain event ID must be a positive integer")
        try:
            return self._projections_by_event[event_id]
        except KeyError:
            raise CaptainProjectionError(
                "Captain projections are unavailable for the selected FPL event"
            ) from None


class FplReviewCsvProjectionSource:
    """Read one documented FPL Review projection export from local storage."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def fetch_event_projections(self, event_id: int) -> tuple[CaptainProjection, ...]:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise CaptainProjectionError("Captain event ID must be a positive integer")
        points_column = f"{event_id}_Pts"
        try:
            with self._path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
                if fieldnames is None or "ID" not in fieldnames or points_column not in fieldnames:
                    raise CaptainProjectionError(
                        f"FPL Review CSV has no projection column for event {event_id}"
                    )
                if len(fieldnames) != len(set(fieldnames)):
                    raise CaptainProjectionError("FPL Review CSV contains duplicate columns")
                return _parse_review_rows(reader, points_column)
        except CaptainProjectionError:
            raise
        except (OSError, csv.Error, UnicodeError):
            raise CaptainProjectionError("FPL Review CSV could not be read safely") from None


def _parse_review_rows(
    rows: Sequence[Mapping[str, str | None]] | csv.DictReader,
    points_column: str,
) -> tuple[CaptainProjection, ...]:
    projections: list[CaptainProjection] = []
    seen_ids: set[int] = set()
    for row_number, row in enumerate(rows, start=2):
        element_id = _parse_element_id(row.get("ID"), row_number)
        if element_id in seen_ids:
            raise CaptainProjectionError(
                f"FPL Review CSV contains duplicate player ID {element_id}"
            )
        seen_ids.add(element_id)
        projected_points = _parse_projected_points(row.get(points_column), row_number)
        projections.append(CaptainProjection(element_id, projected_points))
    if not projections:
        raise CaptainProjectionError("FPL Review CSV contains no projection rows")
    return tuple(projections)


def _parse_element_id(value: str | None, row_number: int) -> int:
    try:
        element_id = int(value or "")
    except ValueError:
        raise CaptainProjectionError(
            f"FPL Review CSV row {row_number} has an invalid player ID"
        ) from None
    if element_id <= 0 or str(element_id) != (value or "").strip():
        raise CaptainProjectionError(f"FPL Review CSV row {row_number} has an invalid player ID")
    return element_id


def _parse_projected_points(value: str | None, row_number: int) -> Decimal:
    try:
        projected_points = Decimal((value or "").strip())
    except InvalidOperation:
        raise CaptainProjectionError(
            f"FPL Review CSV row {row_number} has invalid projected points"
        ) from None
    if not projected_points.is_finite():
        raise CaptainProjectionError(
            f"FPL Review CSV row {row_number} has invalid projected points"
        )
    return projected_points
