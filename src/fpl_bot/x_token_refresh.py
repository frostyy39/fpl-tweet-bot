"""Safe OAuth token refresh and compare-and-swap token-state coordination."""

import base64
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from fpl_bot.x_api import (
    UrllibXHttpTransport,
    XHttpRequest,
    XHttpResponse,
    XHttpTransport,
)
from fpl_bot.x_errors import (
    XTokenConcurrencyError,
    XTokenRefreshError,
    XTokenStateError,
    XTokenStoreError,
    XTransportError,
)
from fpl_bot.x_oauth import (
    OAUTH_SCOPES,
    X_TOKEN_URL,
    OAuthClientCredentials,
    OAuthTokenBundle,
)

DEFAULT_REFRESH_MARGIN = timedelta(minutes=5)
DEFAULT_REFRESH_LEASE_DURATION = timedelta(minutes=1)


@dataclass(frozen=True, slots=True, eq=False)
class XOAuthTokenState:
    """Immutable secret token state; values are excluded from representations."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at_utc: datetime
    token_type: str = "bearer"
    scopes: tuple[str, ...] = OAUTH_SCOPES

    def __post_init__(self) -> None:
        _require_secret(self.access_token, "access token")
        _require_secret(self.refresh_token, "refresh token")
        if not isinstance(self.token_type, str) or self.token_type.casefold() != "bearer":
            raise XTokenStateError("OAuth token state has an invalid token type")
        _require_utc(self.expires_at_utc, "OAuth access-token expiry")
        scopes = _validated_scopes(self.scopes)
        object.__setattr__(self, "token_type", "bearer")
        object.__setattr__(self, "scopes", scopes)

    @classmethod
    def from_oauth_bundle(cls, bundle: OAuthTokenBundle) -> "XOAuthTokenState":
        """Convert the existing manual bootstrap result into runtime token state."""
        _require_utc(bundle.received_at_utc, "OAuth token receipt time")
        if bundle.expires_in_seconds <= 0:
            raise XTokenStateError("OAuth token lifetime must be positive")
        return cls(
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            expires_at_utc=bundle.received_at_utc + timedelta(seconds=bundle.expires_in_seconds),
            token_type=bundle.token_type,
            scopes=bundle.scopes,
        )

    def is_valid_beyond(self, now_utc: datetime, margin: timedelta) -> bool:
        _require_utc(now_utc, "Current token-provider time")
        if not isinstance(margin, timedelta) or margin < timedelta(0):
            raise XTokenStateError("OAuth refresh safety margin cannot be negative")
        return self.expires_at_utc > now_utc + margin


@dataclass(frozen=True, slots=True)
class VersionedXTokenState:
    """One authoritative store snapshot with an opaque compare-and-swap revision."""

    revision: str
    state: XOAuthTokenState = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision:
            raise XTokenStateError("OAuth token-state revision must be non-empty")
        if not isinstance(self.state, XOAuthTokenState):
            raise XTokenStateError("OAuth token store returned invalid state")


@dataclass(frozen=True, slots=True)
class XTokenRefreshLease:
    """Non-secret entitlement to refresh one exact authoritative generation."""

    expected_revision: str
    owner_id: str
    expires_at_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.expected_revision, str) or not self.expected_revision:
            raise XTokenStateError("OAuth refresh lease revision must be non-empty")
        if not isinstance(self.owner_id, str) or not self.owner_id:
            raise XTokenStateError("OAuth refresh lease owner must be non-empty")
        _require_utc(self.expires_at_utc, "OAuth refresh lease expiry")


class XTokenStateStore(Protocol):
    """Mutable secure store requiring distributed compare-and-swap semantics."""

    def read(self) -> VersionedXTokenState: ...

    def replace_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool: ...


class XTokenRefreshCoordinator(Protocol):
    """Distributed lease and authoritative transition around external refresh."""

    def acquire_refresh_lease(
        self,
        expected_revision: str,
        *,
        owner_id: str,
        now_utc: datetime,
        expires_at_utc: datetime,
    ) -> XTokenRefreshLease | None: ...

    def replace_if_revision_with_lease(
        self,
        lease: XTokenRefreshLease,
        replacement: XOAuthTokenState,
    ) -> bool: ...

    def release_refresh_lease(self, lease: XTokenRefreshLease) -> bool: ...


class XOAuthRefreshBoundary(Protocol):
    def refresh(
        self,
        current: XOAuthTokenState,
        credentials: OAuthClientCredentials,
    ) -> XOAuthTokenState: ...


class InMemoryXTokenStateStore:
    """Deterministic test/development store; not a production concurrency mechanism."""

    def __init__(self, initial_state: XOAuthTokenState) -> None:
        if not isinstance(initial_state, XOAuthTokenState):
            raise XTokenStateError("Initial OAuth token state is invalid")
        self._state = initial_state
        self._revision = 1

    def read(self) -> VersionedXTokenState:
        return VersionedXTokenState(str(self._revision), self._state)

    def replace_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool:
        if not isinstance(replacement, XOAuthTokenState):
            raise XTokenStateError("Replacement OAuth token state is invalid")
        if expected_revision != str(self._revision):
            return False
        self._state = replacement
        self._revision += 1
        return True


class XOAuthRefreshClient:
    """Refresh a confidential-client user token through X's sole token endpoint."""

    def __init__(
        self,
        *,
        transport: XHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        _validate_token_endpoint(X_TOKEN_URL)
        self._transport = transport if transport is not None else UrllibXHttpTransport()
        self._timeout_seconds = timeout_seconds
        self._now = now if now is not None else _utc_now

    def refresh(
        self,
        current: XOAuthTokenState,
        credentials: OAuthClientCredentials,
    ) -> XOAuthTokenState:
        if not isinstance(current, XOAuthTokenState):
            raise XTokenStateError("OAuth refresh requires validated current token state")
        if not isinstance(credentials, OAuthClientCredentials) or any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or not value.isprintable()
            for value in (credentials.client_id, credentials.client_secret)
        ):
            raise XTokenRefreshError("Confidential OAuth client credentials are invalid")
        basic_value = base64.b64encode(
            f"{credentials.client_id}:{credentials.client_secret}".encode()
        ).decode("ascii")
        request = XHttpRequest(
            method="POST",
            url=X_TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {basic_value}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token,
                }
            ).encode(),
        )
        try:
            response = self._transport.send(request, self._timeout_seconds)
        except XTransportError:
            raise XTokenRefreshError("X OAuth refresh failed at the network boundary") from None
        if response.status_code != 200:
            raise XTokenRefreshError(f"X OAuth refresh failed with HTTP {response.status_code}")
        return _parse_refresh_response(
            response,
            current=current,
            received_at_utc=self._now(),
        )


class RefreshingXAccessTokenProvider:
    """Return a usable access token, atomically persisting refresh rotation first."""

    def __init__(
        self,
        store: XTokenStateStore,
        refresh_client: XOAuthRefreshBoundary,
        credentials: OAuthClientCredentials,
        *,
        refresh_coordinator: XTokenRefreshCoordinator | None = None,
        clock: Callable[[], datetime] | None = None,
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
        refresh_lease_duration: timedelta = DEFAULT_REFRESH_LEASE_DURATION,
        lease_owner_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(refresh_margin, timedelta) or refresh_margin < timedelta(0):
            raise ValueError("refresh_margin cannot be negative")
        if not isinstance(refresh_lease_duration, timedelta) or refresh_lease_duration <= timedelta(
            0
        ):
            raise ValueError("refresh_lease_duration must be positive")
        self._store = store
        self._refresh_coordinator = refresh_coordinator
        self._refresh_client = refresh_client
        self._credentials = credentials
        self._clock = clock if clock is not None else _utc_now
        self._refresh_margin = refresh_margin
        self._refresh_lease_duration = refresh_lease_duration
        self._lease_owner_factory = lease_owner_factory or _new_lease_owner

    def get_valid_access_token(self) -> str:
        now_utc = self._clock()
        _require_utc(now_utc, "Current token-provider time")
        original = self._read_store()
        if original.state.is_valid_beyond(now_utc, self._refresh_margin):
            return original.state.access_token

        if self._refresh_coordinator is not None:
            return self._refresh_with_distributed_lease(original, now_utc)

        return self._refresh_with_cas(original, now_utc)

    def _refresh_with_distributed_lease(
        self,
        original: VersionedXTokenState,
        now_utc: datetime,
    ) -> str:
        coordinator = self._refresh_coordinator
        if coordinator is None:  # pragma: no cover - guarded by caller
            raise XTokenStoreError("OAuth refresh coordinator is unavailable")
        owner_id = self._lease_owner_factory()
        if not isinstance(owner_id, str) or not owner_id:
            raise XTokenStoreError("OAuth refresh lease owner generation failed")
        try:
            lease = coordinator.acquire_refresh_lease(
                original.revision,
                owner_id=owner_id,
                now_utc=now_utc,
                expires_at_utc=now_utc + self._refresh_lease_duration,
            )
        except XTokenStoreError:
            raise
        except Exception:
            raise XTokenStoreError("OAuth refresh lease could not be acquired") from None
        if lease is None:
            return self._use_concurrent_winner(original, now_utc)

        try:
            reconfirmed = self._read_store()
        except XTokenStoreError:
            self._release_lease(coordinator, lease, suppress_errors=True)
            raise
        if reconfirmed.revision != original.revision:
            self._release_lease(coordinator, lease)
            if reconfirmed.state.is_valid_beyond(now_utc, self._refresh_margin):
                return reconfirmed.state.access_token
            raise XTokenConcurrencyError(
                "Concurrent OAuth refresh did not yield usable authoritative token state"
            )

        try:
            replacement = self._refresh_client.refresh(reconfirmed.state, self._credentials)
        except XTokenRefreshError:
            self._release_lease(coordinator, lease)
            winner = self._read_store()
            if winner.revision != original.revision and winner.state.is_valid_beyond(
                now_utc, self._refresh_margin
            ):
                return winner.state.access_token
            raise

        try:
            self._require_usable_replacement(replacement, now_utc)
        except XTokenRefreshError:
            self._release_lease(coordinator, lease)
            raise
        try:
            replaced = coordinator.replace_if_revision_with_lease(lease, replacement)
        except XTokenStoreError:
            self._release_lease(coordinator, lease, suppress_errors=True)
            raise
        except Exception:
            self._release_lease(coordinator, lease, suppress_errors=True)
            raise XTokenStoreError(
                "Refreshed OAuth token state could not be durably confirmed"
            ) from None
        if not isinstance(replaced, bool):
            self._release_lease(coordinator, lease, suppress_errors=True)
            raise XTokenStoreError("OAuth token store returned an invalid update result")
        if replaced:
            return replacement.access_token
        self._release_lease(coordinator, lease, suppress_errors=True)
        return self._use_concurrent_winner(original, now_utc)

    def _refresh_with_cas(
        self,
        original: VersionedXTokenState,
        now_utc: datetime,
    ) -> str:
        try:
            replacement = self._refresh_client.refresh(original.state, self._credentials)
        except XTokenRefreshError:
            winner = self._read_store()
            if winner.revision != original.revision and winner.state.is_valid_beyond(
                now_utc, self._refresh_margin
            ):
                return winner.state.access_token
            raise

        self._require_usable_replacement(replacement, now_utc)

        try:
            replaced = self._store.replace_if_revision(original.revision, replacement)
        except Exception:
            raise XTokenStoreError(
                "Refreshed OAuth token state could not be durably confirmed"
            ) from None
        if not isinstance(replaced, bool):
            raise XTokenStoreError("OAuth token store returned an invalid update result")
        if replaced:
            return replacement.access_token
        return self._use_concurrent_winner(original, now_utc)

    def _use_concurrent_winner(
        self,
        original: VersionedXTokenState,
        now_utc: datetime,
    ) -> str:
        winner = self._read_store()
        if winner.revision != original.revision and winner.state.is_valid_beyond(
            now_utc, self._refresh_margin
        ):
            return winner.state.access_token
        raise XTokenConcurrencyError(
            "Concurrent OAuth refresh did not yield usable authoritative token state"
        )

    def _require_usable_replacement(
        self,
        replacement: XOAuthTokenState,
        now_utc: datetime,
    ) -> None:
        if not replacement.is_valid_beyond(now_utc, self._refresh_margin):
            raise XTokenRefreshError(
                "X OAuth refresh returned an access token with insufficient usable lifetime"
            )

    @staticmethod
    def _release_lease(
        coordinator: XTokenRefreshCoordinator,
        lease: XTokenRefreshLease,
        *,
        suppress_errors: bool = False,
    ) -> None:
        try:
            released = coordinator.release_refresh_lease(lease)
            if not isinstance(released, bool):
                raise XTokenStoreError("OAuth refresh lease release returned an invalid result")
        except Exception:
            if suppress_errors:
                return
            raise XTokenStoreError("OAuth refresh lease could not be safely released") from None

    def _read_store(self) -> VersionedXTokenState:
        try:
            snapshot = self._store.read()
        except Exception:
            raise XTokenStoreError("Authoritative OAuth token state could not be read") from None
        if not isinstance(snapshot, VersionedXTokenState):
            raise XTokenStoreError("Authoritative OAuth token store returned invalid state")
        return snapshot


def _parse_refresh_response(
    response: XHttpResponse,
    *,
    current: XOAuthTokenState,
    received_at_utc: datetime,
) -> XOAuthTokenState:
    try:
        payload = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise XTokenRefreshError("X OAuth refresh response was not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise XTokenRefreshError("X OAuth refresh response must be a JSON object")

    access_token = _response_token(payload, "access_token", required=True)
    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.casefold() != "bearer":
        raise XTokenRefreshError("X OAuth refresh response has an invalid token type")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise XTokenRefreshError("X OAuth refresh response has invalid expiry data")
    _require_utc(received_at_utc, "OAuth refresh receipt time")

    raw_scope = payload.get("scope")
    scopes = current.scopes if raw_scope is None else _parse_response_scopes(raw_scope)
    replacement_refresh_token = _response_token(payload, "refresh_token", required=False)
    try:
        expires_at_utc = received_at_utc + timedelta(seconds=expires_in)
    except OverflowError as exc:
        raise XTokenRefreshError("X OAuth refresh response expiry is out of range") from exc
    return XOAuthTokenState(
        access_token=access_token,
        refresh_token=replacement_refresh_token or current.refresh_token,
        expires_at_utc=expires_at_utc,
        token_type="bearer",
        scopes=scopes,
    )


def _parse_response_scopes(raw_scope: Any) -> tuple[str, ...]:
    if not isinstance(raw_scope, str) or not raw_scope.strip():
        raise XTokenRefreshError("X OAuth refresh response has invalid scope data")
    scopes = tuple(raw_scope.split())
    if len(scopes) != len(set(scopes)) or not set(OAUTH_SCOPES).issubset(scopes):
        raise XTokenRefreshError("X OAuth refresh response is missing required V1 scopes")
    return scopes


def _validated_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not isinstance(scopes, tuple)
        or not scopes
        or any(not isinstance(scope, str) or not scope for scope in scopes)
        or len(scopes) != len(set(scopes))
        or not set(OAUTH_SCOPES).issubset(scopes)
    ):
        raise XTokenStateError("OAuth token state is missing required V1 scopes")
    return scopes


def _response_token(payload: Mapping[str, Any], name: str, *, required: bool) -> str | None:
    if name not in payload and not required:
        return None
    value = payload.get(name)
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise XTokenRefreshError(f"X OAuth refresh response has no valid {name}")
    return value


def _require_secret(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise XTokenStateError(f"OAuth {label} must be non-empty")


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise XTokenStateError(f"{label} must be timezone-aware UTC")


def _validate_token_endpoint(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.x.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/2/oauth2/token"
        or parsed.query
        or parsed.fragment
    ):
        raise XTokenRefreshError("X OAuth token endpoint configuration is invalid")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_lease_owner() -> str:
    return secrets.token_urlsafe(24)
