"""Allowlisted, bounded evidence for a worker's failed release check."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fpl_bot.captain_orchestration_timing import CaptainTiming, require_utc


class ReleaseRejectionCode(StrEnum):
    RUN_ID_REUSED = "run_id_reused"
    REVOKED = "revoked"
    CANCELLED_BEFORE_REQUEST = "cancelled_before_request"
    UNTYPED_REFUSAL = "untyped_refusal"
    INVALID_REPLY_TYPE = "invalid_reply_type"
    ASSIGNMENT_MISMATCH = "assignment_mismatch"
    RUN_ID_MISMATCH = "run_id_mismatch"
    WORKER_BEFORE_WINDOW = "worker_before_window"
    WORKER_AFTER_WINDOW = "worker_after_window"
    ISSUED_BEFORE_WINDOW = "issued_before_window"
    ISSUED_AFTER_WINDOW = "issued_after_window"
    ISSUED_IN_WORKER_FUTURE = "issued_in_worker_future"
    MALFORMED_RESPONSE = "malformed_response"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"


class WindowPosition(StrEnum):
    BEFORE = "before"
    WITHIN = "within"
    AFTER = "after"
    UNKNOWN = "unknown"


class ClockRelation(StrEnum):
    WORKER_AHEAD = "worker_ahead"
    WORKER_BEHIND = "worker_behind"
    INDETERMINATE = "indeterminate"
    UNKNOWN = "unknown"


_MAX_OFFSET_MS = 60_000


def _bounded_ms(seconds: float) -> int:
    return max(-_MAX_OFFSET_MS, min(_MAX_OFFSET_MS, int(seconds * 1000)))


def _position(timing: CaptainTiming, moment: datetime | None) -> WindowPosition:
    if moment is None:
        return WindowPosition.UNKNOWN
    require_utc(moment)
    if moment < timing.target_utc:
        return WindowPosition.BEFORE
    if moment > timing.expiry_utc:
        return WindowPosition.AFTER
    return WindowPosition.WITHIN


@dataclass(frozen=True, slots=True)
class ReleaseRejectionDiagnostic:
    schema_version: int
    code: ReleaseRejectionCode
    worker_window: WindowPosition
    issued_window: WindowPosition
    clock_relation: ClockRelation
    issued_minus_worker_ms: int | None
    server_minus_worker_lower_ms: int | None
    server_minus_worker_upper_ms: int | None

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not isinstance(self.code, ReleaseRejectionCode)
            or not isinstance(self.worker_window, WindowPosition)
            or not isinstance(self.issued_window, WindowPosition)
            or not isinstance(self.clock_relation, ClockRelation)
        ):
            raise ValueError("invalid release diagnostic classification")
        for value in (
            self.issued_minus_worker_ms,
            self.server_minus_worker_lower_ms,
            self.server_minus_worker_upper_ms,
        ):
            if value is not None and (type(value) is not int or abs(value) > _MAX_OFFSET_MS):
                raise ValueError("invalid bounded release offset")
        pair = (self.server_minus_worker_lower_ms, self.server_minus_worker_upper_ms)
        if (pair[0] is None) != (pair[1] is None) or (pair[0] is not None and pair[0] > pair[1]):
            raise ValueError("invalid release clock interval")
        if (pair[0] is None) != (self.clock_relation == ClockRelation.UNKNOWN):
            raise ValueError("release clock relation lacks evidence")
        if pair[0] is not None:
            expected = (
                ClockRelation.WORKER_BEHIND
                if pair[0] > 0
                else ClockRelation.WORKER_AHEAD
                if pair[1] < 0
                else ClockRelation.INDETERMINATE
            )
            if self.clock_relation != expected:
                raise ValueError("release clock relation contradicts bounds")
        required_position = {
            ReleaseRejectionCode.WORKER_BEFORE_WINDOW: (self.worker_window, WindowPosition.BEFORE),
            ReleaseRejectionCode.WORKER_AFTER_WINDOW: (self.worker_window, WindowPosition.AFTER),
            ReleaseRejectionCode.ISSUED_BEFORE_WINDOW: (self.issued_window, WindowPosition.BEFORE),
            ReleaseRejectionCode.ISSUED_AFTER_WINDOW: (self.issued_window, WindowPosition.AFTER),
        }.get(self.code)
        if required_position is not None and required_position[0] != required_position[1]:
            raise ValueError("release code contradicts window evidence")
        if self.code == ReleaseRejectionCode.ISSUED_IN_WORKER_FUTURE and (
            self.issued_minus_worker_ms is None or self.issued_minus_worker_ms < 0
        ):
            raise ValueError("release code contradicts issued-time evidence")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "worker_window": self.worker_window.value,
            "issued_window": self.issued_window.value,
            "clock_relation": self.clock_relation.value,
            "issued_minus_worker_ms": self.issued_minus_worker_ms,
            "server_minus_worker_lower_ms": self.server_minus_worker_lower_ms,
            "server_minus_worker_upper_ms": self.server_minus_worker_upper_ms,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ReleaseRejectionDiagnostic":
        if type(value) is not dict or set(value) != {
            "schema_version",
            "code",
            "worker_window",
            "issued_window",
            "clock_relation",
            "issued_minus_worker_ms",
            "server_minus_worker_lower_ms",
            "server_minus_worker_upper_ms",
        }:
            raise ValueError("invalid release diagnostic payload")
        try:
            return cls(
                value["schema_version"],
                ReleaseRejectionCode(value["code"]),
                WindowPosition(value["worker_window"]),
                WindowPosition(value["issued_window"]),
                ClockRelation(value["clock_relation"]),
                value["issued_minus_worker_ms"],
                value["server_minus_worker_lower_ms"],
                value["server_minus_worker_upper_ms"],
            )
        except (TypeError, ValueError):
            raise ValueError("invalid release diagnostic payload") from None


def release_diagnostic(
    code: ReleaseRejectionCode,
    timing: CaptainTiming | None,
    request_started: datetime | None,
    worker_now: datetime | None,
    issued_at: datetime | None = None,
    server_sent_at: datetime | None = None,
) -> ReleaseRejectionDiagnostic:
    """A server timestamp bounds clock offset by the request/response interval.

    Network delay makes a crossing-zero interval indeterminate, not proof of skew.
    No raw timestamps, URLs, grants or exception text enter the diagnostic.
    """
    if not isinstance(code, ReleaseRejectionCode):
        raise ValueError("invalid release diagnostic code")
    for value in (request_started, worker_now, issued_at, server_sent_at):
        if value is not None:
            require_utc(value)
    lower = upper = None
    if (
        request_started is not None
        and worker_now is not None
        and server_sent_at is not None
        and request_started <= worker_now
    ):
        lower = _bounded_ms((server_sent_at - worker_now).total_seconds())
        upper = _bounded_ms((server_sent_at - request_started).total_seconds())
    relation = (
        ClockRelation.UNKNOWN
        if lower is None
        else ClockRelation.WORKER_BEHIND
        if lower > 0
        else ClockRelation.WORKER_AHEAD
        if upper < 0
        else ClockRelation.INDETERMINATE
    )
    return ReleaseRejectionDiagnostic(
        1,
        code,
        _position(timing, worker_now) if timing else WindowPosition.UNKNOWN,
        _position(timing, issued_at) if timing else WindowPosition.UNKNOWN,
        relation,
        _bounded_ms((issued_at - worker_now).total_seconds())
        if issued_at is not None and worker_now is not None
        else None,
        lower,
        upper,
    )
