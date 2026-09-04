"""Fixed, read-only HTTP matrix for diagnosing public FPL reachability."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from fpl_bot.api import FPL_REQUEST_HEADERS

FPL_PUBLIC_ORIGIN = "https://fantasy.premierleague.com"
FPL_HTTP_TIMEOUT_SECONDS = 10.0

PRODUCTION_HEADER_PROFILE = dict(FPL_REQUEST_HEADERS)
BROWSER_STANDARD_HEADER_PROFILE = {
    **FPL_REQUEST_HEADERS,
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-GB,en;q=0.9",
}
HEADER_PROFILES = (
    ("production", PRODUCTION_HEADER_PROFILE),
    ("browser_standard", BROWSER_STANDARD_HEADER_PROFILE),
)

_SAFE_RESPONSE_HEADERS = {
    "Server": "server",
    "Via": "via",
    "Date": "date",
    "Content-Type": "content_type",
    "Content-Length": "content_length",
    "CF-Ray": "cf_ray",
    "CF-Mitigated": "cf_mitigated",
    "X-Cache": "x_cache",
    "X-Cache-Hits": "x_cache_hits",
    "X-Served-By": "x_served_by",
    "X-Timer": "x_timer",
    "X-Varnish": "x_varnish",
    "X-Request-ID": "x_request_id",
}

Opener = Callable[..., object]


@dataclass(frozen=True, slots=True)
class FplHttpObservation:
    """Strictly non-sensitive metadata for one predetermined public request."""

    endpoint: str
    header_profile: str
    http_status: int | None
    final_url: str | None
    response_headers: Mapping[str, str]
    category: str | None = None
    redirected_off_origin: bool = False

    def fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "endpoint": self.endpoint,
            "header_profile": self.header_profile,
        }
        if self.http_status is not None:
            fields["http_status"] = self.http_status
        if self.final_url is not None:
            fields["final_url"] = self.final_url
        if self.response_headers:
            fields["response_headers"] = dict(self.response_headers)
        if self.category is not None:
            fields["category"] = self.category
        if self.redirected_off_origin:
            fields["redirected_off_origin"] = True
        return fields


class FplHttpMatrixProbe:
    """Compare fixed public endpoints without reading or retaining response bodies."""

    def __init__(
        self,
        *,
        timeout_seconds: float = FPL_HTTP_TIMEOUT_SECONDS,
        opener: Opener = urlopen,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def run(self, *, event_id: int) -> tuple[FplHttpObservation, ...]:
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("event_id must be a positive integer")

        endpoints = (
            ("bootstrap", f"{FPL_PUBLIC_ORIGIN}/api/bootstrap-static/"),
            (
                "fixtures",
                f"{FPL_PUBLIC_ORIGIN}/api/fixtures/?{urlencode({'event': event_id})}",
            ),
            ("site_root", f"{FPL_PUBLIC_ORIGIN}/"),
        )
        return tuple(
            self._observe(endpoint, url, profile, headers)
            for profile, headers in HEADER_PROFILES
            for endpoint, url in endpoints
        )

    def _observe(
        self,
        endpoint: str,
        url: str,
        profile: str,
        headers: Mapping[str, str],
    ) -> FplHttpObservation:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            response = self._opener(request, timeout=self._timeout_seconds)
        except HTTPError as error:
            try:
                return self._http_observation(
                    endpoint,
                    profile,
                    error.code,
                    error.geturl(),
                    error.headers,
                )
            finally:
                error.close()
        except (URLError, TimeoutError, OSError):
            return FplHttpObservation(
                endpoint,
                profile,
                None,
                None,
                {},
                category="transport_error",
            )

        try:
            return self._http_observation(
                endpoint,
                profile,
                int(response.getcode()),
                response.geturl(),
                response.headers,
            )
        finally:
            response.close()

    @staticmethod
    def _http_observation(
        endpoint: str,
        profile: str,
        status: int,
        final_url: str,
        headers: Mapping[str, str] | None,
    ) -> FplHttpObservation:
        allowed_final_url = _allow_public_fpl_url(final_url)
        return FplHttpObservation(
            endpoint,
            profile,
            status,
            allowed_final_url,
            _allowlisted_response_headers(headers, base_url=final_url),
            redirected_off_origin=allowed_final_url is None,
        )


def _allowlisted_response_headers(
    headers: Mapping[str, str] | None,
    *,
    base_url: str,
) -> dict[str, str]:
    if headers is None:
        return {}

    result: dict[str, str] = {}
    for source_name, output_name in _SAFE_RESPONSE_HEADERS.items():
        value = headers.get(source_name)
        if value is not None and (safe_value := _safe_header_value(value)) is not None:
            result[output_name] = safe_value

    location = headers.get("Location")
    if location is not None:
        safe_location = _allow_public_fpl_url(urljoin(base_url, location))
        if safe_location is not None:
            result["location"] = safe_location
    return result


def _allow_public_fpl_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "fantasy.premierleague.com":
        return None
    if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
        return None
    return value


def _safe_header_value(value: str) -> str | None:
    stripped = value.strip()
    if not stripped or len(stripped) > 300:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in stripped):
        return None
    return stripped
