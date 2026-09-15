"""Immutable, allowlisted Captain assignment and projection handoff schema v1."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from fpl_bot.captain_orchestration_timing import CaptainTiming, require_utc, utc_text

SCHEMA_VERSION = 1


def _positive(value: int, *, zero: bool = False) -> None:
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError("invalid integer field")


def _uuid(value: UUID) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError("invalid identity")


def _text(value: str, limit: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError("invalid public text field")


def _decimal_text(value: Decimal) -> str:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or len(value.as_tuple().digits) > 64
        or abs(value.as_tuple().exponent) > 64
        or value.adjusted() > 63
    ):
        raise ValueError("invalid projection")
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _keys(value: object, expected: set[str]) -> dict:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("schema fields do not match")
    return value


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)",
        value,
    ):
        raise ValueError("invalid UTC timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid UTC timestamp") from None


def _parse_id(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("invalid identity")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("invalid identity") from None
    if str(parsed) != value:
        raise ValueError("identity must use canonical UUID form")
    return parsed


@dataclass(frozen=True, slots=True)
class CaptainAssignment:
    assignment_id: UUID
    generation_id: UUID
    event_id: int
    event_code: str
    timing: CaptainTiming

    def __post_init__(self) -> None:
        _uuid(self.assignment_id)
        _uuid(self.generation_id)
        _positive(self.event_id)
        if not isinstance(self.event_code, str) or not re.fullmatch(
            r"(?:GW|BGW|DGW|BDGW)" + str(self.event_id), self.event_code
        ):
            raise ValueError("event code does not match event identity")
        if not isinstance(self.timing, CaptainTiming):
            raise ValueError("invalid timing")

    def to_payload(self) -> dict:
        return {
            "assignment_id": str(self.assignment_id),
            "generation_id": str(self.generation_id),
            "event_id": self.event_id,
            "event_code": self.event_code,
            "deadline_utc": utc_text(self.timing.deadline_utc),
            "target_utc": utc_text(self.timing.target_utc),
            "release_utc": utc_text(self.timing.release_utc),
            "warmup_utc": utc_text(self.timing.warmup_utc),
            "expiry_utc": utc_text(self.timing.expiry_utc),
        }

    @classmethod
    def from_payload(cls, value: object) -> "CaptainAssignment":
        data = _keys(
            value,
            {
                "assignment_id",
                "generation_id",
                "event_id",
                "event_code",
                "deadline_utc",
                "target_utc",
                "release_utc",
                "warmup_utc",
                "expiry_utc",
            },
        )
        result = cls(
            _parse_id(data["assignment_id"]),
            _parse_id(data["generation_id"]),
            data["event_id"],
            data["event_code"],
            CaptainTiming(_parse_time(data["deadline_utc"])),
        )
        for name in ("target_utc", "release_utc", "warmup_utc", "expiry_utc"):
            if _parse_time(data[name]) != getattr(result.timing, name):
                raise ValueError("inconsistent assignment timing")
        return result


class AuthenticationStatus(StrEnum):
    AUTHENTICATED = "authenticated"
    REQUIRED = "authentication_required"
    NOT_CONFIRMED = "not_confirmed"


class CleanupStatus(StrEnum):
    RELEASED = "released"
    IN_USE = "in_use"
    UNCLEAN = "unclean"


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    source_ordinal: int
    review_name: str
    review_team: str
    projected_points: Decimal
    official_element_id: int | None = None

    def __post_init__(self) -> None:
        _positive(self.source_ordinal)
        _text(self.review_name, 128)
        if not isinstance(self.review_team, str) or not re.fullmatch(
            r"[A-Z]{2,4}", self.review_team
        ):
            raise ValueError("invalid team code")
        _decimal_text(self.projected_points)
        if self.official_element_id is not None:
            _positive(self.official_element_id)

    def to_payload(self) -> dict:
        return {
            "source_ordinal": self.source_ordinal,
            "review_name": self.review_name,
            "review_team": self.review_team,
            "projected_points": _decimal_text(self.projected_points),
            "official_element_id": self.official_element_id,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ProjectionRecord":
        data = _keys(
            value,
            {
                "source_ordinal",
                "review_name",
                "review_team",
                "projected_points",
                "official_element_id",
            },
        )
        points = data["projected_points"]
        if (
            not isinstance(points, str)
            or len(points) > 130
            or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", points)
        ):
            raise ValueError("projection must be a finite decimal string")
        return cls(
            data["source_ordinal"],
            data["review_name"],
            data["review_team"],
            Decimal(points),
            data["official_element_id"],
        )


@dataclass(frozen=True, slots=True)
class RowCounts:
    raw: int
    genuine: int
    auxiliary: int
    extracted: int

    def __post_init__(self) -> None:
        for value in (self.raw, self.genuine, self.auxiliary, self.extracted):
            _positive(value, zero=True)
        if self.raw != self.genuine + self.auxiliary or self.extracted > self.genuine:
            raise ValueError("inconsistent row counts")


@dataclass(frozen=True, slots=True)
class ProjectionHandoff:
    schema_version: int
    assignment: CaptainAssignment
    attempt_id: UUID
    acquisition_started_utc: datetime
    acquisition_ended_utc: datetime
    records: tuple[ProjectionRecord, ...]
    counts: RowCounts
    completeness: CompletenessStatus
    authentication: AuthenticationStatus
    cleanup: CleanupStatus

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported handoff schema version")
        if not isinstance(self.assignment, CaptainAssignment) or not isinstance(
            self.counts, RowCounts
        ):
            raise ValueError("invalid handoff structure")
        _uuid(self.attempt_id)
        require_utc(self.acquisition_started_utc)
        require_utc(self.acquisition_ended_utc)
        if self.acquisition_ended_utc < self.acquisition_started_utc:
            raise ValueError("acquisition ended before it started")
        if type(self.records) is not tuple or not all(
            isinstance(r, ProjectionRecord) for r in self.records
        ):
            raise ValueError("records must be an immutable typed tuple")
        for value, kind in (
            (self.completeness, CompletenessStatus),
            (self.authentication, AuthenticationStatus),
            (self.cleanup, CleanupStatus),
        ):
            if not isinstance(value, kind):
                raise ValueError("invalid status classification")
        if len(self.records) != self.counts.extracted:
            raise ValueError("extracted count mismatch")
        if self.completeness == CompletenessStatus.COMPLETE and self.counts.genuine != len(
            self.records
        ):
            raise ValueError("complete set is missing genuine rows")
        ordinals = [r.source_ordinal for r in self.records]
        if ordinals != sorted(set(ordinals)):
            raise ValueError("source ordinals must be unique and increasing")
        names = [
            (" ".join(unicodedata.normalize("NFC", r.review_name).split()), r.review_team)
            for r in self.records
        ]
        ids = [r.official_element_id for r in self.records if r.official_element_id is not None]
        if len(names) != len(set(names)) or len(ids) != len(set(ids)):
            raise ValueError("duplicate record identity")

    def require_eligible(self, current_assignment: CaptainAssignment, now_utc: datetime) -> None:
        """Pure precondition, not authority to post or proof of server-side session validity."""
        require_utc(now_utc)
        timing = self.assignment.timing
        if self.assignment != current_assignment:
            raise ValueError("stale assignment generation")
        if (
            not timing.permits_posting_acquisition(self.acquisition_started_utc)
            or not timing.permits_new_attempt(now_utc)
            or not self.acquisition_started_utc <= self.acquisition_ended_utc <= now_utc
        ):
            raise ValueError("outside release window")
        if (
            self.authentication != AuthenticationStatus.AUTHENTICATED
            or self.cleanup != CleanupStatus.RELEASED
            or self.completeness != CompletenessStatus.COMPLETE
            or not self.records
        ):
            raise ValueError("handoff not eligible")

    def to_payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "assignment": self.assignment.to_payload(),
            "attempt_id": str(self.attempt_id),
            "acquisition_started_utc": utc_text(self.acquisition_started_utc),
            "acquisition_ended_utc": utc_text(self.acquisition_ended_utc),
            "records": [r.to_payload() for r in self.records],
            "counts": {
                k: getattr(self.counts, k) for k in ("raw", "genuine", "auxiliary", "extracted")
            },
            "completeness": self.completeness.value,
            "authentication": self.authentication.value,
            "cleanup": self.cleanup.value,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ProjectionHandoff":
        data = _keys(
            value,
            {
                "schema_version",
                "assignment",
                "attempt_id",
                "acquisition_started_utc",
                "acquisition_ended_utc",
                "records",
                "counts",
                "completeness",
                "authentication",
                "cleanup",
            },
        )
        counts = _keys(data["counts"], {"raw", "genuine", "auxiliary", "extracted"})
        if type(data["records"]) is not list:
            raise ValueError("records must be an array")
        try:
            completeness = CompletenessStatus(data["completeness"])
            authentication = AuthenticationStatus(data["authentication"])
            cleanup = CleanupStatus(data["cleanup"])
        except (ValueError, TypeError):
            raise ValueError("invalid status classification") from None
        return cls(
            data["schema_version"],
            CaptainAssignment.from_payload(data["assignment"]),
            _parse_id(data["attempt_id"]),
            _parse_time(data["acquisition_started_utc"]),
            _parse_time(data["acquisition_ended_utc"]),
            tuple(ProjectionRecord.from_payload(r) for r in data["records"]),
            RowCounts(**counts),
            completeness,
            authentication,
            cleanup,
        )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def verify_digest(self, expected: str) -> None:
        if expected != self.payload_digest:
            raise ValueError("payload digest mismatch")
