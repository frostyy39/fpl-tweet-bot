from collections.abc import Callable
from typing import Any

import pytest

from fpl_bot.api import FplApiClient
from fpl_bot.errors import FplApiError


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


def test_api_client_queries_event_filtered_fixture_endpoint() -> None:
    calls: list[tuple[str, float]] = []
    client = FplApiClient(timeout_seconds=4.5, opener=make_opener(b"[]", calls))

    assert client.fetch_event_fixtures(7) == []
    assert calls == [("https://fantasy.premierleague.com/api/fixtures/?event=7", 4.5)]


def test_api_client_rejects_invalid_json() -> None:
    client = FplApiClient(opener=make_opener(b"not-json", []))

    with pytest.raises(FplApiError, match="invalid JSON"):
        client.fetch_bootstrap_static()


def test_api_client_rejects_wrong_bootstrap_shape() -> None:
    client = FplApiClient(opener=make_opener(b"[]", []))

    with pytest.raises(FplApiError, match="JSON object"):
        client.fetch_bootstrap_static()
