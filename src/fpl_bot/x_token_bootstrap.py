"""One-shot local-to-cloud bootstrap for the approved X OAuth token state."""

import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from fpl_bot.cloud_token_store import InitialTokenStateResult, InitialTokenStateStatus
from fpl_bot.windows_dpapi import WindowsDpapiProtector
from fpl_bot.x_errors import XTokenStateError, XTokenStoreError
from fpl_bot.x_oauth import DPAPI_FILE_MAGIC, OAUTH_SCOPES, OAuthTokenBundle
from fpl_bot.x_token_refresh import VersionedXTokenState, XOAuthTokenState

LOCAL_HANDOFF_SCHEMA_VERSION = 1
BOOTSTRAP_REFRESH_MARGIN = timedelta(minutes=5)


class TokenUnprotector(Protocol):
    def unprotect(self, ciphertext: bytes) -> bytes: ...


class InitialTokenStateStore(Protocol):
    def initialize(self, initial_state: XOAuthTokenState) -> InitialTokenStateResult: ...

    def read(self) -> VersionedXTokenState: ...


@dataclass(frozen=True, slots=True)
class ValidatedLocalTokenState:
    x_user_id: str
    state: XOAuthTokenState = field(repr=False)


@dataclass(frozen=True, slots=True)
class XTokenBootstrapResult:
    initialization: InitialTokenStateResult
    token_type: str
    expires_at_utc: datetime
    scopes: tuple[str, ...]
    access_token_status: str
    refresh_token_present: bool = field(default=True, repr=False)


class LocalDpapiTokenStateReader:
    """Strictly parse the existing external current-user DPAPI handoff."""

    def __init__(
        self,
        *,
        repository_root: Path,
        unprotector: TokenUnprotector | None = None,
    ) -> None:
        self._repository_root = repository_root.resolve(strict=True)
        self._unprotector = unprotector or WindowsDpapiProtector()

    def read(self, path: Path, *, expected_user_id: str) -> ValidatedLocalTokenState:
        target = self._validated_path(path)
        try:
            protected = target.read_bytes()
        except OSError:
            raise XTokenStateError("Local OAuth token handoff could not be read") from None
        if not protected.startswith(DPAPI_FILE_MAGIC):
            raise XTokenStateError("Local OAuth token handoff is not a supported DPAPI file")
        ciphertext = protected[len(DPAPI_FILE_MAGIC) :]
        try:
            plaintext = self._unprotector.unprotect(ciphertext)
        except Exception:
            raise XTokenStateError("Local OAuth token handoff could not be decrypted") from None
        return _parse_local_handoff(plaintext, expected_user_id=expected_user_id)

    def _validated_path(self, path: Path) -> Path:
        if not isinstance(path, Path) or not path.is_absolute():
            raise XTokenStateError("Local OAuth token handoff path must be absolute")
        if path.is_symlink():
            raise XTokenStateError("Local OAuth token handoff must not be a symbolic link")
        try:
            target = path.resolve(strict=True)
        except OSError:
            raise XTokenStateError("Local OAuth token handoff does not exist") from None
        if target == self._repository_root or self._repository_root in target.parents:
            raise XTokenStateError("Local OAuth token handoff must remain outside the repository")
        if not target.is_file() or target.is_symlink():
            raise XTokenStateError("Local OAuth token handoff must be a regular file")
        return target


def bootstrap_x_token_state(
    local_state: ValidatedLocalTokenState,
    store: InitialTokenStateStore,
    *,
    now_utc: datetime,
) -> XTokenBootstrapResult:
    """Initialize authority once, then verify through production store semantics."""

    _require_utc(now_utc, "OAuth bootstrap time")
    initialization = store.initialize(local_state.state)
    try:
        authoritative = store.read()
    except Exception:
        raise XTokenStoreError("Bootstrapped OAuth token state could not be verified") from None
    if authoritative.revision != initialization.revision:
        raise XTokenStoreError("Bootstrapped OAuth token revision could not be verified")
    if initialization.status is InitialTokenStateStatus.INITIALIZED and not _states_match(
        authoritative.state, local_state.state
    ):
        raise XTokenStoreError("Bootstrapped OAuth token metadata did not match local state")
    return XTokenBootstrapResult(
        initialization=initialization,
        token_type=authoritative.state.token_type,
        expires_at_utc=authoritative.state.expires_at_utc,
        scopes=authoritative.state.scopes,
        access_token_status=_access_token_status(authoritative.state, now_utc),
    )


def _parse_local_handoff(
    plaintext: bytes,
    *,
    expected_user_id: str,
) -> ValidatedLocalTokenState:
    try:
        raw = json.loads(plaintext, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise XTokenStateError("Local OAuth token handoff is malformed") from None
    required = {
        "version",
        "x_user_id",
        "x_username",
        "token_type",
        "scope",
        "expires_in",
        "received_at_utc",
        "access_token",
        "refresh_token",
    }
    if not isinstance(raw, Mapping) or set(raw) != required:
        raise XTokenStateError("Local OAuth token handoff has an invalid shape")
    if raw.get("version") != LOCAL_HANDOFF_SCHEMA_VERSION:
        raise XTokenStateError("Local OAuth token handoff has an unsupported schema")
    user_id = raw.get("x_user_id")
    if (
        not isinstance(expected_user_id, str)
        or not expected_user_id.isdigit()
        or expected_user_id.startswith("0")
        or user_id != expected_user_id
    ):
        raise XTokenStateError("Local OAuth token handoff does not match the expected X user")
    username = raw.get("x_username")
    if not isinstance(username, str) or not username or not username.isprintable():
        raise XTokenStateError("Local OAuth token handoff has invalid identity metadata")
    scope = raw.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        raise XTokenStateError("Local OAuth token handoff has invalid scopes")
    scopes = tuple(scope.split())
    if len(scopes) != len(set(scopes)) or not set(OAUTH_SCOPES).issubset(scopes):
        raise XTokenStateError("Local OAuth token handoff is missing required V1 scopes")
    expires_in = raw.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise XTokenStateError("Local OAuth token handoff has invalid expiry data")
    received_at = _parse_utc(raw.get("received_at_utc"))
    try:
        bundle = OAuthTokenBundle(
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            token_type=raw["token_type"],
            scopes=scopes,
            expires_in_seconds=expires_in,
            received_at_utc=received_at,
        )
        state = XOAuthTokenState.from_oauth_bundle(bundle)
    except (KeyError, TypeError, OverflowError, XTokenStateError):
        raise XTokenStateError("Local OAuth token handoff contains invalid token state") from None
    return ValidatedLocalTokenState(x_user_id=user_id, state=state)


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise XTokenStateError("Local OAuth token receipt time is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise XTokenStateError("Local OAuth token receipt time is malformed") from None
    _require_utc(parsed, "Local OAuth token receipt time")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _states_match(left: XOAuthTokenState, right: XOAuthTokenState) -> bool:
    return (
        secrets.compare_digest(left.access_token, right.access_token)
        and secrets.compare_digest(left.refresh_token, right.refresh_token)
        and left.expires_at_utc == right.expires_at_utc
        and left.token_type == right.token_type
        and left.scopes == right.scopes
    )


def _access_token_status(state: XOAuthTokenState, now_utc: datetime) -> str:
    if state.expires_at_utc <= now_utc:
        return "expired"
    if not state.is_valid_beyond(now_utc, BOOTSTRAP_REFRESH_MARGIN):
        return "near_expiry"
    return "current"


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise XTokenStateError(f"{label} must be timezone-aware UTC")


def utc_now() -> datetime:
    return datetime.now(UTC)


def default_repository_root() -> Path:
    return Path(os.getcwd()).resolve(strict=True)
