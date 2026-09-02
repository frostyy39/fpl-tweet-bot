"""Official X API v2 user-context client with fail-closed posting guards."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import (
    XAmbiguousWriteError,
    XApiResponseError,
    XAuthenticationError,
    XConfigurationError,
    XIdentityMismatchError,
    XPermissionError,
    XRateLimitError,
    XRequestRejectedError,
    XResponseValidationError,
    XTransportError,
)

X_API_BASE_URL = "https://api.x.com/"
X_USER_AGENT = "fpl-tweet-bot/0.2"
X_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")
X_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
DEFINITE_CREATE_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 422, 429})


@dataclass(frozen=True, slots=True)
class AuthenticatedXUser:
    user_id: str
    username: str


@dataclass(frozen=True, slots=True)
class CreatedXPost:
    post_id: str
    text: str


@dataclass(frozen=True, slots=True)
class XHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class XHttpResponse:
    status_code: int
    body: bytes = field(repr=False)


class XPostCreator(Protocol):
    """Injectable boundary for guarded text-Post creation."""

    def create_text_post(self, text: str) -> CreatedXPost: ...


class XAccessTokenProvider(Protocol):
    """Supply a validated current user-context access token."""

    def get_valid_access_token(self) -> str: ...


class XIdentityReader(Protocol):
    """Read-only boundary for the authenticated X user."""

    def get_authenticated_user(self) -> AuthenticatedXUser: ...


class XHttpTransport(Protocol):
    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects so Authorization is never forwarded to another destination."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class UrllibXHttpTransport:
    """Standard-library transport with redirects and retries disabled."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def send(self, request: XHttpRequest, timeout_seconds: float) -> XHttpResponse:
        urllib_request = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with self._opener.open(urllib_request, timeout=timeout_seconds) as response:
                return XHttpResponse(status_code=response.status, body=response.read())
        except HTTPError as exc:
            try:
                body = exc.read()
            except (HTTPException, OSError):
                body = b""
            return XHttpResponse(status_code=exc.code, body=body)
        except (HTTPException, URLError, TimeoutError, OSError) as exc:
            raise XTransportError("X API network request failed") from exc


class XIdentityClient:
    """Retrieve only the authenticated X identity through API v2."""

    def __init__(
        self,
        config: XPostingConfig,
        *,
        timeout_seconds: float = 10.0,
        transport: XHttpTransport | None = None,
        token_provider: XAccessTokenProvider | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._transport = transport if transport is not None else UrllibXHttpTransport()
        self._token_provider = token_provider

    def get_authenticated_user(self) -> AuthenticatedXUser:
        token = self._get_valid_access_token()
        return self._get_authenticated_user(token)

    def _get_authenticated_user(self, token: str) -> AuthenticatedXUser:
        request = XHttpRequest(
            method="GET",
            url=f"{X_API_BASE_URL}2/users/me",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": X_USER_AGENT,
            },
        )
        response = self._transport.send(request, self._timeout_seconds)
        self._raise_for_read_status(response)
        payload = _decode_json(response.body)
        return _parse_authenticated_user(payload)

    def _get_valid_access_token(self) -> str:
        token = (
            self._token_provider.get_valid_access_token()
            if self._token_provider is not None
            else self._config.require_user_access_token()
        )
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or not token.isprintable()
        ):
            raise XConfigurationError("X access-token provider returned no valid credential")
        return token

    @staticmethod
    def _raise_for_read_status(response: XHttpResponse) -> None:
        if response.status_code == 200:
            return
        if 400 <= response.status_code < 500:
            XIdentityClient._raise_for_definite_rejection(response)
        raise XApiResponseError(
            _http_error_message("X API read failed", response),
            response.status_code,
        )

    @staticmethod
    def _raise_for_definite_rejection(response: XHttpResponse) -> None:
        message = _http_error_message("X API rejected the request", response)
        if response.status_code == 401:
            raise XAuthenticationError(message, response.status_code)
        if response.status_code == 403:
            raise XPermissionError(message, response.status_code)
        if response.status_code == 429:
            raise XRateLimitError(message, response.status_code)
        raise XRequestRejectedError(message, response.status_code)


class XApiClient(XIdentityClient):
    """Create guarded text Posts after the inherited read-only identity check."""

    def create_text_post(self, text: str) -> CreatedXPost:
        """Create exactly the supplied text after all configuration and identity guards pass."""
        _validate_post_text(text)
        expected_user_id = self._config.require_posting_identity_guard()
        token = self._get_valid_access_token()
        authenticated_user = self._get_authenticated_user(token)
        if authenticated_user.user_id != expected_user_id:
            raise XIdentityMismatchError(
                "Authenticated X user ID does not match the configured expected user ID; "
                "posting prohibited"
            )

        body = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")).encode()
        request = XHttpRequest(
            method="POST",
            url=f"{X_API_BASE_URL}2/tweets",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": X_USER_AGENT,
            },
            body=body,
        )
        try:
            response = self._transport.send(request, self._timeout_seconds)
        except XTransportError as exc:
            raise XAmbiguousWriteError(
                "X create-Post connection failed after the write was attempted; outcome may be "
                "ambiguous and must not be retried automatically"
            ) from exc

        if response.status_code in DEFINITE_CREATE_REJECTION_STATUSES:
            self._raise_for_definite_rejection(response)
        if response.status_code != 201:
            raise XAmbiguousWriteError(
                f"X returned unexpected HTTP {response.status_code} after create-Post; "
                "outcome may be ambiguous and must not be retried automatically"
            )

        try:
            payload = _decode_json(response.body)
            return _parse_created_post(payload, expected_text=text)
        except XResponseValidationError as exc:
            raise XAmbiguousWriteError(
                "X returned HTTP 201 without a fully validated create-Post response; "
                "do not retry automatically"
            ) from exc


def _decode_json(body: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise XResponseValidationError("X API returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise XResponseValidationError("X API response must be a JSON object")
    return payload


def _parse_authenticated_user(payload: Mapping[str, Any]) -> AuthenticatedXUser:
    _reject_embedded_errors(payload)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise XResponseValidationError("X authenticated-user response must contain a data object")
    user_id = data.get("id")
    username = data.get("username")
    if not isinstance(user_id, str) or not X_ID_PATTERN.fullmatch(user_id):
        raise XResponseValidationError("X authenticated-user ID must be a positive numeric string")
    if not isinstance(username, str) or not X_USERNAME_PATTERN.fullmatch(username):
        raise XResponseValidationError("X authenticated-user username is malformed")
    return AuthenticatedXUser(user_id=user_id, username=username)


def _parse_created_post(payload: Mapping[str, Any], *, expected_text: str) -> CreatedXPost:
    _reject_embedded_errors(payload)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise XResponseValidationError("X create-Post response must contain a data object")
    post_id = data.get("id")
    text = data.get("text")
    if not isinstance(post_id, str) or not X_ID_PATTERN.fullmatch(post_id):
        raise XResponseValidationError("X create-Post response has no valid Post ID")
    if text != expected_text:
        raise XResponseValidationError("X create-Post response text does not match the request")
    return CreatedXPost(post_id=post_id, text=text)


def _reject_embedded_errors(payload: Mapping[str, Any]) -> None:
    errors = payload.get("errors")
    if errors:
        raise XResponseValidationError("X API success response unexpectedly contains errors")


def _validate_post_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")


def _http_error_message(prefix: str, response: XHttpResponse) -> str:
    detail = _safe_problem_detail(response.body)
    suffix = f": {detail}" if detail else ""
    return f"{prefix} with HTTP {response.status_code}{suffix}"


def _safe_problem_detail(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    candidates: list[Mapping[str, Any]] = [payload]
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        candidates.insert(0, errors[0])
    for candidate in candidates:
        for key in ("detail", "title"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return None
