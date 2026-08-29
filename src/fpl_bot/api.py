"""Read-only access to live Fantasy Premier League data."""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fpl_bot.errors import FplApiError

FPL_API_BASE_URL = "https://fantasy.premierleague.com/api/"
USER_AGENT = "fpl-tweet-bot/0.1 (+read-only dry run)"

Opener = Callable[..., Any]


class FplApiClient:
    """Minimal client for the public JSON endpoints hosted by FPL."""

    def __init__(
        self,
        base_url: str = FPL_API_BASE_URL,
        timeout_seconds: float = 10.0,
        opener: Opener = urlopen,
    ) -> None:
        self._base_url = f"{base_url.rstrip('/')}/"
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def fetch_bootstrap_static(self) -> Mapping[str, Any]:
        payload = self._get_json("bootstrap-static/")
        if not isinstance(payload, Mapping):
            raise FplApiError("FPL bootstrap response must be a JSON object")
        return payload

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")

        query = urlencode({"event": event_id})
        payload = self._get_json(f"fixtures/?{query}")
        if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
            raise FplApiError("FPL fixtures response must be a JSON array of objects")
        return payload

    def _get_json(self, relative_url: str) -> Any:
        request = Request(
            f"{self._base_url}{relative_url}",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise FplApiError(
                f"Unable to retrieve FPL data from {request.full_url}: {exc}"
            ) from exc

        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FplApiError(f"FPL returned invalid JSON from {request.full_url}") from exc
