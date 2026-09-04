import inspect
import json
import sys
from email.message import Message
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError

import fpl_bot.fpl_http_probe as probe_module
import fpl_bot.fpl_http_probe_cli as cli_module
from fpl_bot.api import FPL_REQUEST_HEADERS
from fpl_bot.fpl_http_probe import (
    BROWSER_STANDARD_HEADER_PROFILE,
    FplHttpMatrixProbe,
    FplHttpObservation,
)


class BodyRejectingResponse:
    def __init__(
        self,
        url: str,
        status: int = 200,
        headers: Message | None = None,
    ) -> None:
        self._url = url
        self._status = status
        self.headers = headers or Message()
        self.closed = False

    def getcode(self) -> int:
        return self._status

    def geturl(self) -> str:
        return self._url

    def read(self) -> bytes:
        raise AssertionError("the HTTP matrix must never read a response body")

    def close(self) -> None:
        self.closed = True


def test_matrix_uses_only_fixed_endpoints_and_two_fixed_header_profiles() -> None:
    calls: list[tuple[str, str, dict[str, str], float]] = []

    def opener(request: Any, timeout: float) -> BodyRejectingResponse:
        calls.append(
            (request.full_url, request.get_method(), dict(request.header_items()), timeout)
        )
        return BodyRejectingResponse(request.full_url)

    observations = FplHttpMatrixProbe(opener=opener).run(event_id=3)

    assert len(observations) == 6
    assert [call[0] for call in calls] == [
        "https://fantasy.premierleague.com/api/bootstrap-static/",
        "https://fantasy.premierleague.com/api/fixtures/?event=3",
        "https://fantasy.premierleague.com/",
    ] * 2
    assert {call[1] for call in calls} == {"GET"}
    assert {call[3] for call in calls} == {10.0}
    assert {key.lower(): value for key, value in calls[0][2].items()} == {
        key.lower(): value for key, value in FPL_REQUEST_HEADERS.items()
    }
    assert {key.lower(): value for key, value in calls[3][2].items()} == {
        key.lower(): value for key, value in BROWSER_STANDARD_HEADER_PROFILE.items()
    }


def test_http_error_retains_only_allowlisted_metadata_and_never_reads_body() -> None:
    headers = Message()
    headers["Server"] = "openresty"
    headers["Via"] = "1.1 varnish"
    headers["Content-Type"] = "text/html"
    headers["Content-Length"] = "123"
    headers["CF-Ray"] = "safe-request-id"
    headers["Set-Cookie"] = "private-cookie"
    headers["X-Untrusted"] = "private-value"
    errors: list[HTTPError] = []

    def opener(request: Any, timeout: float) -> BodyRejectingResponse:
        error = HTTPError(request.full_url, 403, "private reason", headers, BytesIO(b"secret"))
        errors.append(error)
        raise error

    observation = FplHttpMatrixProbe(opener=opener).run(event_id=3)[0]

    assert observation.fields() == {
        "endpoint": "bootstrap",
        "header_profile": "production",
        "http_status": 403,
        "final_url": "https://fantasy.premierleague.com/api/bootstrap-static/",
        "response_headers": {
            "server": "openresty",
            "via": "1.1 varnish",
            "content_type": "text/html",
            "content_length": "123",
            "cf_ray": "safe-request-id",
        },
    }
    rendered = str(observation.fields())
    assert "private-cookie" not in rendered
    assert "private-value" not in rendered
    assert "private reason" not in rendered
    assert "secret" not in rendered
    assert errors[0].closed


def test_redirect_metadata_allows_only_public_fpl_urls() -> None:
    same_origin = Message()
    same_origin["Location"] = "/api/bootstrap-static/"
    off_origin = Message()
    off_origin["Location"] = "https://example.invalid/private"
    responses = iter(
        (
            BodyRejectingResponse("https://fantasy.premierleague.com/", headers=same_origin),
            BodyRejectingResponse("https://example.invalid/private", headers=off_origin),
            *(BodyRejectingResponse("https://fantasy.premierleague.com/") for _ in range(4)),
        )
    )

    def opener(request: Any, timeout: float) -> BodyRejectingResponse:
        return next(responses)

    observations = FplHttpMatrixProbe(opener=opener).run(event_id=3)

    assert observations[0].response_headers["location"] == (
        "https://fantasy.premierleague.com/api/bootstrap-static/"
    )
    assert observations[1].final_url is None
    assert observations[1].redirected_off_origin is True


def test_transport_failure_is_classified_without_exception_detail() -> None:
    secret = "private-network-detail"

    def opener(request: Any, timeout: float) -> BodyRejectingResponse:
        raise URLError(secret)

    observation = FplHttpMatrixProbe(opener=opener).run(event_id=3)[0]

    assert observation.fields() == {
        "endpoint": "bootstrap",
        "header_profile": "production",
        "category": "transport_error",
    }
    assert secret not in str(observation.fields())


def test_probe_rejects_invalid_event_id_before_any_request() -> None:
    def opener(request: Any, timeout: float) -> BodyRejectingResponse:
        raise AssertionError("invalid input must not make a request")

    probe = FplHttpMatrixProbe(opener=opener)

    for value in (True, 0, -1):
        try:
            probe.run(event_id=value)
        except ValueError:
            pass
        else:  # pragma: no cover - explicit assertion without a pytest dependency
            raise AssertionError("invalid event ID was accepted")


def test_http_probe_has_no_mutating_or_x_capability() -> None:
    source = inspect.getsource(probe_module)

    for forbidden in (
        "CloudTasks",
        "Firestore",
        "SecretManager",
        "OAuth",
        "XApiClient",
        "create_text_post",
        "2/tweets",
        "DeadlineTaskArmer",
        "PreflightTaskArmer",
    ):
        assert forbidden not in source


def test_cli_emits_only_structured_observations(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    observation = FplHttpObservation(
        "bootstrap",
        "production",
        403,
        "https://fantasy.premierleague.com/api/bootstrap-static/",
        {"server": "openresty"},
    )

    class FakeProbe:
        def run(self, *, event_id: int) -> tuple[FplHttpObservation, ...]:
            assert event_id == 3
            return (observation,)

    monkeypatch.setattr(cli_module, "FplHttpMatrixProbe", FakeProbe)
    monkeypatch.setattr(sys, "argv", ["fpl-bot-fpl-http-probe", "--event-id", "3"])

    assert cli_module.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "result": "http_matrix_complete",
        "observations": [observation.fields()],
    }
