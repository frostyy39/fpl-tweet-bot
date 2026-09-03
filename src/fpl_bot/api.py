"""Read-only access to live Fantasy Premier League data."""

import json
import ssl
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fpl_bot.errors import (
    FplApiHttpError,
    FplApiInvalidJsonError,
    FplApiPayloadValidationError,
    FplApiTimeoutError,
    FplApiTlsError,
    FplApiTransportError,
)

FPL_API_BASE_URL = "https://fantasy.premierleague.com/api/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
FPL_REQUEST_HEADERS = {"Accept": "application/json", "User-Agent": USER_AGENT}

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
            raise FplApiPayloadValidationError("FPL bootstrap response must be a JSON object")
        return payload

    def fetch_event_fixtures(self, event_id: int) -> Sequence[Mapping[str, Any]]:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")

        query = urlencode({"event": event_id})
        payload = self._get_json(f"fixtures/?{query}")
        if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
            raise FplApiPayloadValidationError(
                "FPL fixtures response must be a JSON array of objects"
            )
        return payload

    def _get_json(self, relative_url: str) -> Any:
        request = Request(
            f"{self._base_url}{relative_url}",
            headers=FPL_REQUEST_HEADERS,
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                body = response.read()
        except HTTPError as exc:
            raise FplApiHttpError(exc.code) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise _safe_transport_error(exc) from None

        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise FplApiInvalidJsonError("FPL returned invalid JSON") from None


def _safe_transport_error(error: Exception) -> FplApiTransportError:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, TimeoutError):
        return FplApiTimeoutError("FPL request timed out")
    if isinstance(reason, ssl.SSLError):
        return FplApiTlsError("FPL TLS validation failed")
    return FplApiTransportError("FPL request transport failed")
