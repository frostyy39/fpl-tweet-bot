import ssl
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from fpl_bot.api import FplApiClient
from fpl_bot.errors import (
    FplApiError,
    FplApiHttpError,
    FplApiInvalidJsonError,
    FplApiTimeoutError,
    FplApiTlsError,
    FplApiTransportError,
)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def make_opener(body: bytes, calls: list[tuple[str, float]]) -> Callable[..., Any]:
    def opener(request: Any, timeout: float) -> FakeResponse:
        calls.append((request.full_url, timeout))
        return FakeResponse(body)

    return opener


def raising_opener(error: Exception) -> Callable[..., Any]:
    def opener(*_args: object, **_kwargs: object) -> FakeResponse:
        raise error

    return opener


def test_api_client_queries_event_filtered_fixture_endpoint() -> None:
    calls: list[tuple[str, float]] = []
    client = FplApiClient(timeout_seconds=4.5, opener=make_opener(b"[]", calls))

    assert client.fetch_event_fixtures(7) == []
    assert calls == [("https://fantasy.premierleague.com/api/fixtures/?event=7", 4.5)]


def test_api_client_rejects_invalid_json() -> None:
    client = FplApiClient(opener=make_opener(b"not-json", []))

    with pytest.raises(FplApiError, match="invalid JSON"):
        client.fetch_bootstrap_static()

    with pytest.raises(FplApiInvalidJsonError):
        client.fetch_bootstrap_static()


def test_api_client_rejects_wrong_bootstrap_shape() -> None:
    client = FplApiClient(opener=make_opener(b"[]", []))

    with pytest.raises(FplApiError, match="JSON object"):
        client.fetch_bootstrap_static()


def test_api_client_classifies_timeout_without_transport_detail() -> None:
    secret = "sensitive-proxy-detail"
    client = FplApiClient(opener=raising_opener(TimeoutError(secret)))

    with pytest.raises(FplApiTimeoutError) as raised:
        client.fetch_bootstrap_static()

    assert secret not in str(raised.value)


def test_api_client_classifies_tls_failure_without_transport_detail() -> None:
    secret = "sensitive-certificate-detail"
    error = URLError(ssl.SSLCertVerificationError(1, secret))
    client = FplApiClient(opener=raising_opener(error))

    with pytest.raises(FplApiTlsError) as raised:
        client.fetch_bootstrap_static()

    assert secret not in str(raised.value)


def test_api_client_classifies_other_transport_failure_without_detail() -> None:
    secret = "sensitive-dns-detail"
    client = FplApiClient(opener=raising_opener(URLError(secret)))

    with pytest.raises(FplApiTransportError) as raised:
        client.fetch_bootstrap_static()

    assert secret not in str(raised.value)


def test_api_client_classifies_http_status_without_response_body() -> None:
    secret = "sensitive-response-body"
    error = HTTPError(
        "https://fantasy.premierleague.com/api/bootstrap-static/",
        503,
        secret,
        None,
        None,
    )
    client = FplApiClient(opener=raising_opener(error))

    with pytest.raises(FplApiHttpError) as raised:
        client.fetch_bootstrap_static()

    assert raised.value.status_code == 503
    assert secret not in str(raised.value)
