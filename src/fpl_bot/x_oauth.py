"""OAuth 2.0 PKCE primitives for local X test-account authorization."""

import base64
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from fpl_bot.windows_dpapi import WindowsDpapiProtector
from fpl_bot.x_api import (
    AuthenticatedXUser,
    UrllibXHttpTransport,
    XApiClient,
    XHttpRequest,
    XHttpResponse,
    XHttpTransport,
)
from fpl_bot.x_config import XPostingConfig
from fpl_bot.x_errors import (
    XOAuthCallbackError,
    XOAuthConfigurationError,
    XOAuthHandoffError,
    XOAuthTokenExchangeError,
    XTransportError,
)

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
OAUTH_REDIRECT_URI = "http://127.0.0.1:8765/callback"
OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 8765
OAUTH_CALLBACK_PATH = "/callback"
OAUTH_SCOPES = ("tweet.read", "users.read", "tweet.write", "offline.access")

X_OAUTH_CLIENT_ID_VARIABLE = "X_OAUTH_CLIENT_ID"
X_OAUTH_CLIENT_SECRET_VARIABLE = "X_OAUTH_CLIENT_SECRET"
X_OAUTH_TOKEN_OUTPUT_FILE_VARIABLE = "X_OAUTH_TOKEN_OUTPUT_FILE"

_PKCE_VALUE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
DPAPI_FILE_MAGIC = b"FPLBOT-DPAPI-V1\x00"


@dataclass(frozen=True, slots=True)
class OAuthClientCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LocalOAuthConfig:
    credentials: OAuthClientCredentials = field(repr=False)
    token_output_file: Path = field(repr=False)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "LocalOAuthConfig":
        source = os.environ if environ is None else environ
        client_id = _required_environment_value(source, X_OAUTH_CLIENT_ID_VARIABLE)
        client_secret = _required_environment_value(source, X_OAUTH_CLIENT_SECRET_VARIABLE)
        output_value = _required_environment_value(source, X_OAUTH_TOKEN_OUTPUT_FILE_VARIABLE)
        output_file = Path(output_value)
        if not output_file.is_absolute():
            raise XOAuthConfigurationError(
                f"{X_OAUTH_TOKEN_OUTPUT_FILE_VARIABLE} must be an absolute path "
                "outside the repository"
            )
        return cls(
            credentials=OAuthClientCredentials(client_id=client_id, client_secret=client_secret),
            token_output_file=output_file,
        )


@dataclass(frozen=True, slots=True)
class OAuthAttempt:
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    code_challenge: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthorizationCode:
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthTokenBundle:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    token_type: str
    scopes: tuple[str, ...]
    expires_in_seconds: int
    received_at_utc: datetime


class AuthorizationCodeReceiver(Protocol):
    def receive(
        self,
        expected_state: str,
        on_listening: Callable[[], None],
    ) -> AuthorizationCode: ...


class OAuthTokenHandoff(Protocol):
    def store(self, tokens: OAuthTokenBundle, user: AuthenticatedXUser) -> None: ...


class TokenProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...


def generate_oauth_attempt(
    entropy: Callable[[int], bytes] = secrets.token_bytes,
) -> OAuthAttempt:
    state = _base64url(entropy(32))
    code_verifier = _base64url(entropy(64))
    if not _PKCE_VALUE_PATTERN.fullmatch(code_verifier):
        raise XOAuthConfigurationError("Generated PKCE verifier does not meet length requirements")
    challenge_digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return OAuthAttempt(
        state=state,
        code_verifier=code_verifier,
        code_challenge=_base64url(challenge_digest),
    )


def build_authorization_url(client_id: str, attempt: OAuthAttempt) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": " ".join(OAUTH_SCOPES),
            "state": attempt.state,
            "code_challenge": attempt.code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{X_AUTHORIZE_URL}?{query}"


def parse_callback_target(target: str, expected_state: str) -> AuthorizationCode:
    parsed = urlsplit(target)
    if parsed.path != OAUTH_CALLBACK_PATH:
        raise XOAuthCallbackError("OAuth callback used an unexpected path")
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    returned_state = _single_callback_parameter(parameters, "state")
    if not secrets.compare_digest(returned_state, expected_state):
        raise XOAuthCallbackError("OAuth callback state did not match; authorization rejected")
    if "error" in parameters:
        _single_callback_parameter(parameters, "error")
        raise XOAuthCallbackError("X authorization was denied or failed")
    return AuthorizationCode(value=_single_callback_parameter(parameters, "code"))


class XOAuthTokenClient:
    """Exchange one authorization code using confidential-client Basic authentication."""

    def __init__(
        self,
        *,
        transport: XHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._transport = transport if transport is not None else UrllibXHttpTransport()
        self._timeout_seconds = timeout_seconds
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def exchange_authorization_code(
        self,
        code: AuthorizationCode,
        attempt: OAuthAttempt,
        credentials: OAuthClientCredentials,
    ) -> OAuthTokenBundle:
        basic_value = base64.b64encode(
            f"{credentials.client_id}:{credentials.client_secret}".encode()
        ).decode("ascii")
        body = urlencode(
            {
                "code": code.value,
                "grant_type": "authorization_code",
                "redirect_uri": OAUTH_REDIRECT_URI,
                "code_verifier": attempt.code_verifier,
            }
        ).encode()
        request = XHttpRequest(
            method="POST",
            url=X_TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {basic_value}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        try:
            response = self._transport.send(request, self._timeout_seconds)
        except XTransportError as exc:
            raise XOAuthTokenExchangeError(
                "X OAuth token exchange failed at the network boundary; restart authorization"
            ) from exc
        if response.status_code != 200:
            raise XOAuthTokenExchangeError(
                f"X OAuth token exchange failed with HTTP {response.status_code}"
            )
        return _parse_token_response(response, received_at_utc=self._now())


class ExclusiveTokenFileHandoff:
    """Persist one external handoff, using current-user DPAPI on Windows."""

    def __init__(
        self,
        target: Path,
        *,
        repository_root: Path,
        platform_name: str = os.name,
        windows_protector: TokenProtector | None = None,
    ) -> None:
        self._target = target
        self._repository_root = repository_root
        self._platform_name = platform_name
        self._windows_protector = windows_protector
        self._validated_target()

    def store(self, tokens: OAuthTokenBundle, user: AuthenticatedXUser) -> None:
        target = self._validated_target()
        plaintext = self._serialize(tokens, user)
        file_payload = self._prepare_file_payload(plaintext, tokens)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags, 0o600)
        except OSError:
            raise XOAuthHandoffError(
                "Could not exclusively create the configured external token handoff file"
            ) from None
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(file_payload)
                stream.flush()
                os.fsync(stream.fileno())
            if self._platform_name != "nt":
                os.chmod(target, 0o600)
        except (OSError, TypeError, ValueError):
            target.unlink(missing_ok=True)
            raise XOAuthHandoffError("Could not complete the external token handoff") from None

    def _serialize(self, tokens: OAuthTokenBundle, user: AuthenticatedXUser) -> bytes:
        payload = {
            "version": 1,
            "x_user_id": user.user_id,
            "x_username": user.username,
            "token_type": tokens.token_type,
            "scope": " ".join(tokens.scopes),
            "expires_in": tokens.expires_in_seconds,
            "received_at_utc": tokens.received_at_utc.isoformat(),
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
        }
        return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    def _prepare_file_payload(
        self,
        plaintext: bytes,
        tokens: OAuthTokenBundle,
    ) -> bytes:
        if self._platform_name != "nt":
            return plaintext
        protector = (
            self._windows_protector
            if self._windows_protector is not None
            else WindowsDpapiProtector()
        )
        try:
            protected = protector.protect(plaintext)
        except Exception:
            raise XOAuthHandoffError(
                "Windows DPAPI could not protect the token handoff; no token file was written"
            ) from None
        if not isinstance(protected, bytes) or not protected:
            raise XOAuthHandoffError(
                "Windows DPAPI returned no encrypted token handoff; no token file was written"
            )
        secret_values = (tokens.access_token.encode(), tokens.refresh_token.encode())
        if any(secret_value in protected for secret_value in secret_values):
            raise XOAuthHandoffError(
                "Windows DPAPI output failed plaintext safety validation; no token file was written"
            )
        return DPAPI_FILE_MAGIC + protected

    def _validated_target(self) -> Path:
        if not self._target.is_absolute():
            raise XOAuthHandoffError("Token handoff path must be absolute")
        if self._target.exists() or self._target.is_symlink():
            raise XOAuthHandoffError("Token handoff file already exists; refusing to overwrite it")
        target = self._target.resolve(strict=False)
        repository_root = self._repository_root.resolve(strict=True)
        if target == repository_root or repository_root in target.parents:
            raise XOAuthHandoffError("Token handoff file must be outside the repository")
        if not target.parent.is_dir():
            raise XOAuthHandoffError("Token handoff parent directory does not exist")
        return target


def authorize_test_account(
    config: LocalOAuthConfig,
    *,
    receiver: AuthorizationCodeReceiver,
    handoff: OAuthTokenHandoff,
    browser_open: Callable[[str], bool],
    token_transport: XHttpTransport | None = None,
    identity_transport: XHttpTransport | None = None,
    entropy: Callable[[int], bytes] = secrets.token_bytes,
    now: Callable[[], datetime] | None = None,
) -> AuthenticatedXUser:
    attempt = generate_oauth_attempt(entropy)
    authorization_url = build_authorization_url(config.credentials.client_id, attempt)

    def open_authorization_page() -> None:
        if not browser_open(authorization_url):
            raise XOAuthCallbackError("Could not open the X authorization page")

    code = receiver.receive(attempt.state, open_authorization_page)
    tokens = XOAuthTokenClient(transport=token_transport, now=now).exchange_authorization_code(
        code,
        attempt,
        config.credentials,
    )
    identity_client = XApiClient(
        XPostingConfig(posting_enabled=False, user_access_token=tokens.access_token),
        transport=identity_transport,
    )
    user = identity_client.get_authenticated_user()
    handoff.store(tokens, user)
    return user


def _parse_token_response(
    response: XHttpResponse,
    *,
    received_at_utc: datetime,
) -> OAuthTokenBundle:
    try:
        payload = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise XOAuthTokenExchangeError("X OAuth token response was not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise XOAuthTokenExchangeError("X OAuth token response must be a JSON object")
    access_token = _required_token_value(payload, "access_token")
    refresh_token = _required_token_value(payload, "refresh_token")
    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.casefold() != "bearer":
        raise XOAuthTokenExchangeError("X OAuth token response has an invalid token type")
    raw_scope = payload.get("scope")
    if not isinstance(raw_scope, str):
        raise XOAuthTokenExchangeError("X OAuth token response has no scope")
    returned_scopes = raw_scope.split()
    if len(returned_scopes) != len(set(returned_scopes)) or set(returned_scopes) != set(
        OAUTH_SCOPES
    ):
        raise XOAuthTokenExchangeError("X OAuth token response scopes do not match the request")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise XOAuthTokenExchangeError("X OAuth token response has invalid expiry data")
    if received_at_utc.tzinfo is None or received_at_utc.utcoffset() != UTC.utcoffset(None):
        raise XOAuthTokenExchangeError("Token receipt time must be timezone-aware UTC")
    return OAuthTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        scopes=OAUTH_SCOPES,
        expires_in_seconds=expires_in,
        received_at_utc=received_at_utc,
    )


def _single_callback_parameter(parameters: Mapping[str, list[str]], name: str) -> str:
    values = parameters.get(name)
    if values is None or len(values) != 1 or not values[0]:
        raise XOAuthCallbackError(f"OAuth callback must contain exactly one non-empty {name}")
    return values[0]


def _required_token_value(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or value.isspace():
        raise XOAuthTokenExchangeError(f"X OAuth token response has no valid {name}")
    return value


def _required_environment_value(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if value is None or not value or value != value.strip() or not value.isprintable():
        raise XOAuthConfigurationError(f"{name} is required and must be a non-empty value")
    return value


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
