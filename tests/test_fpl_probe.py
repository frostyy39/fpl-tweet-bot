import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

import fpl_bot.fpl_probe as probe_module
import fpl_bot.fpl_probe_cli as cli_module
from fpl_bot.errors import (
    DeadlineEventSelectionError,
    FplApiHttpError,
    FplApiInvalidJsonError,
    FplApiTimeoutError,
    FplApiTlsError,
    FplApiTransportError,
    FplBootstrapValidationError,
    MultipleSameDayEventsError,
    NoSuitableEventError,
)
from fpl_bot.fpl_diagnostics import (
    FplDiagnosticCategory,
    FplFailureDiagnostic,
    diagnose_fpl_failure,
)
from fpl_bot.fpl_probe import FplReadOnlyProbe

EVENT_ID = 3
DEADLINE_UTC = datetime(2026, 9, 4, 17, 30, tzinfo=UTC)


class StaticSource:
    def __init__(self, payload: Mapping[str, Any] | Exception) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        self.calls.append("bootstrap")
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def fetch_event_fixtures(self, event_id: int) -> list[Mapping[str, Any]]:
        self.calls.append(f"fixtures:{event_id}")
        raise AssertionError("the planning probe must not fetch fixtures")


def bootstrap() -> dict[str, Any]:
    return {
        "events": [
            {
                "id": EVENT_ID,
                "name": "Gameweek 3",
                "deadline_time": "2026-09-04T17:30:00Z",
                "is_current": False,
                "is_next": True,
            }
        ]
    }


def test_non_today_probe_returns_public_deadline_metadata_only() -> None:
    source = StaticSource(bootstrap())
    probe = FplReadOnlyProbe(source, clock=lambda: datetime(2026, 9, 3, 12, 0, tzinfo=UTC))

    result = probe.run()

    assert result.event.event_id == EVENT_ID
    assert result.event.deadline_utc == DEADLINE_UTC
    assert result.deadline_london.isoformat() == "2026-09-04T18:30:00+01:00"
    assert result.is_current_london_day is False
    assert source.calls == ["bootstrap"]


def test_same_day_probe_is_still_read_only() -> None:
    source = StaticSource(bootstrap())
    probe = FplReadOnlyProbe(source, clock=lambda: datetime(2026, 9, 4, 5, 0, tzinfo=UTC))

    result = probe.run()

    assert result.is_current_london_day is True
    assert source.calls == ["bootstrap"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FplApiTransportError("private detail"), ("fpl_bootstrap", "transport_error", None)),
        (FplApiTlsError("private detail"), ("fpl_bootstrap", "tls_error", None)),
        (FplApiTimeoutError("private detail"), ("fpl_bootstrap", "timeout", None)),
        (FplApiHttpError(503), ("fpl_bootstrap", "http_error", 503)),
        (FplApiInvalidJsonError("private detail"), ("fpl_bootstrap", "invalid_json", None)),
        (
            FplBootstrapValidationError("private detail"),
            ("fpl_bootstrap", "payload_validation", None),
        ),
        (NoSuitableEventError("private detail"), ("event_selection", "no_event", None)),
        (
            MultipleSameDayEventsError("private detail"),
            ("event_selection", "multiple_same_day_events", None),
        ),
        (
            DeadlineEventSelectionError("private detail"),
            ("event_selection", "event_selection_failure", None),
        ),
        (RuntimeError("private detail"), ("checker", "unexpected_internal", None)),
    ],
)
def test_failure_categories_are_allowlisted(
    error: Exception,
    expected: tuple[str, str, int | None],
) -> None:
    fields = diagnose_fpl_failure(error).fields()

    assert fields["stage"] == expected[0]
    assert fields["category"] == expected[1]
    assert fields.get("http_status") == expected[2]
    assert set(fields) <= {"stage", "category", "http_status"}
    assert "private detail" not in json.dumps(fields)


def test_arbitrary_strings_cannot_become_diagnostic_fields() -> None:
    with pytest.raises(TypeError, match="allowlisted"):
        FplFailureDiagnostic("private-stage", FplDiagnosticCategory.TRANSPORT_ERROR)  # type: ignore[arg-type]


def test_cli_success_emits_only_public_observation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = StaticSource(bootstrap())
    probe = FplReadOnlyProbe(source, clock=lambda: datetime(2026, 9, 3, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(cli_module, "create_fpl_probe", lambda: probe)

    assert cli_module.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "result": "success",
        "selected_event_id": 3,
        "official_deadline_utc": "2026-09-04T17:30:00+00:00",
        "deadline_london": "2026-09-04T18:30:00+01:00",
        "deadline_is_today_london": False,
    }


def test_cli_failure_never_emits_raw_exception_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "response-body-or-credential-value"
    probe = FplReadOnlyProbe(StaticSource(FplApiTransportError(secret)))
    monkeypatch.setattr(cli_module, "create_fpl_probe", lambda: probe)

    assert cli_module.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "result": "probe_failed",
        "stage": "fpl_bootstrap",
        "category": "transport_error",
    }
    assert secret not in captured.err


def test_probe_dependency_graph_has_no_task_state_oauth_or_post_capability() -> None:
    source = inspect.getsource(probe_module) + inspect.getsource(cli_module)

    for forbidden in (
        "CloudTasks",
        "Firestore",
        "PostingState",
        "XApiClient",
        "OAuth",
        "create_text_post",
        "2/tweets",
        "DeadlineTaskArmer",
        "PreflightTaskArmer",
    ):
        assert forbidden not in source
