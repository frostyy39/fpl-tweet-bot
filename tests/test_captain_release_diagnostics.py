from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from fpl_bot.captain_orchestration_timing import CaptainTiming
from fpl_bot.captain_release_diagnostics import (
    ClockRelation,
    ReleaseRejectionDiagnostic,
    WindowPosition,
    release_diagnostic,
)
from fpl_bot.captain_release_diagnostics import (
    ReleaseRejectionCode as Code,
)

T = datetime(2026, 10, 10, 8, tzinfo=UTC)
TIMING = CaptainTiming(T + timedelta(hours=2))


@pytest.mark.parametrize(
    "request_started,received,server,expected",
    [
        (
            T,
            T + timedelta(milliseconds=100),
            T + timedelta(milliseconds=150),
            ClockRelation.WORKER_BEHIND,
        ),
        (
            T,
            T + timedelta(milliseconds=100),
            T - timedelta(milliseconds=50),
            ClockRelation.WORKER_AHEAD,
        ),
        (
            T,
            T + timedelta(milliseconds=100),
            T + timedelta(milliseconds=50),
            ClockRelation.INDETERMINATE,
        ),
    ],
)
def test_server_timestamp_bounds_clock_relation_without_treating_latency_as_skew(
    request_started, received, server, expected
):
    result = release_diagnostic(Code.REVOKED, TIMING, request_started, received, server, server)
    assert result.clock_relation == expected
    assert result.server_minus_worker_lower_ms == int((server - received).total_seconds() * 1000)
    assert result.server_minus_worker_upper_ms == int(
        (server - request_started).total_seconds() * 1000
    )
    assert ReleaseRejectionDiagnostic.from_payload(result.to_payload()) == result


def test_large_clock_error_is_bounded_and_precisely_classified():
    result = release_diagnostic(
        Code.WORKER_BEFORE_WINDOW,
        TIMING,
        T - timedelta(hours=1),
        T - timedelta(hours=1),
        T,
        T,
    )
    assert result.clock_relation == ClockRelation.WORKER_BEHIND
    assert result.server_minus_worker_lower_ms == 60_000
    assert result.server_minus_worker_upper_ms == 60_000
    assert result.worker_window == WindowPosition.BEFORE
    assert result.issued_window == WindowPosition.WITHIN


def test_missing_server_timestamp_is_unknown_not_inferred_from_issued_at():
    result = release_diagnostic(Code.REVOKED, TIMING, T, T + timedelta(milliseconds=50))
    assert result.clock_relation == ClockRelation.UNKNOWN
    assert result.server_minus_worker_lower_ms is None


def test_diagnostic_rejects_extra_fields_malformed_bounds_and_secret_text():
    result = release_diagnostic(Code.REVOKED, TIMING, T, T)
    payload = result.to_payload()
    with pytest.raises(ValueError):
        ReleaseRejectionDiagnostic.from_payload({**payload, "cookie": "SECRET"})
    with pytest.raises(ValueError):
        ReleaseRejectionDiagnostic.from_payload({**payload, "code": "TOKEN_SECRET"})
    with pytest.raises(ValueError):
        replace(result, issued_minus_worker_ms=60_001)
    with pytest.raises(ValueError):
        replace(result, code=Code.WORKER_AFTER_WINDOW)
    assert "SECRET" not in str(result.to_payload())
