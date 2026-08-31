"""Distributed X OAuth token state using Firestore authority and Secret Manager data."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fpl_bot.firestore_state import FirestoreClient, TransactionalWrapper
from fpl_bot.x_errors import (
    XTokenAuthorityPersistenceError,
    XTokenAuthorityUnconfirmedError,
    XTokenSecretStorageError,
    XTokenStateError,
    XTokenStoreError,
)
from fpl_bot.x_token_refresh import (
    VersionedXTokenState,
    XOAuthTokenState,
    XTokenRefreshLease,
)

TOKEN_PAYLOAD_SCHEMA_VERSION = 1
TOKEN_METADATA_SCHEMA_VERSION = 1
DEFAULT_TOKEN_METADATA_COLLECTION = "x_oauth_token_authority"
DEFAULT_REFRESH_LEASE_DURATION = timedelta(minutes=1)

PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}\Z")
SECRET_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
COLLECTION_ID_PATTERN = re.compile(r"[^/]{1,1500}\Z")
USER_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")


class _Snapshot(Protocol):
    exists: bool

    def to_dict(self) -> Mapping[str, Any] | None: ...


class _DocumentReference(Protocol):
    def get(self, *, transaction: Any | None = None) -> _Snapshot: ...


class _CollectionReference(Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...


class _Transaction(Protocol):
    def update(self, reference: _DocumentReference, fields: Mapping[str, Any]) -> None: ...


class SecretManagerClient(Protocol):
    def access_secret_version(self, request: Mapping[str, Any]) -> Any: ...

    def add_secret_version(self, request: Mapping[str, Any]) -> Any: ...

    def disable_secret_version(self, request: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class CloudXTokenStateStoreConfig:
    """Validated non-secret resource identity for one configured X account."""

    project_id: str
    secret_id: str
    expected_user_id: str
    metadata_collection: str = DEFAULT_TOKEN_METADATA_COLLECTION
    lease_duration: timedelta = DEFAULT_REFRESH_LEASE_DURATION

    def __post_init__(self) -> None:
        _require_pattern(self.project_id, PROJECT_ID_PATTERN, "GCP project ID")
        _require_pattern(self.secret_id, SECRET_ID_PATTERN, "X token secret ID")
        _require_pattern(self.expected_user_id, USER_ID_PATTERN, "expected X user ID")
        _require_pattern(
            self.metadata_collection,
            COLLECTION_ID_PATTERN,
            "X token metadata collection",
        )
        if not isinstance(self.lease_duration, timedelta) or self.lease_duration <= timedelta(0):
            raise XTokenStateError("OAuth refresh lease duration must be positive")

    @property
    def secret_name(self) -> str:
        return f"projects/{self.project_id}/secrets/{self.secret_id}"

    @property
    def metadata_document_id(self) -> str:
        return f"x-user-{self.expected_user_id}"

    def validate_version_name(self, value: Any) -> str:
        prefix = f"{self.secret_name}/versions/"
        if (
            not isinstance(value, str)
            or not value.startswith(prefix)
            or USER_ID_PATTERN.fullmatch(value.removeprefix(prefix)) is None
        ):
            raise XTokenStateError(
                "OAuth token authority must name an explicit numeric Secret Manager version"
            )
        return value


@dataclass(frozen=True, slots=True)
class _TokenAuthorityMetadata:
    revision: int
    secret_version_name: str
    previous_secret_version_name: str | None
    updated_at_utc: datetime
    refresh_lease_owner: str | None
    refresh_lease_expires_at_utc: datetime | None


class GoogleCloudXTokenStateStore:
    """Secure token generations plus transactional Firestore authority and leases."""

    def __init__(
        self,
        config: CloudXTokenStateStoreConfig,
        *,
        firestore_client: FirestoreClient,
        secret_manager_client: SecretManagerClient,
        transactional_wrapper: TransactionalWrapper | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, CloudXTokenStateStoreConfig):
            raise XTokenStateError("Cloud OAuth token-store configuration is invalid")
        self._config = config
        self._firestore_client = firestore_client
        self._reference = firestore_client.collection(config.metadata_collection).document(
            config.metadata_document_id
        )
        self._secrets = secret_manager_client
        self._transactional = transactional_wrapper or _default_transactional_wrapper()
        self._clock = clock or _utc_now

    def read(self) -> VersionedXTokenState:
        metadata = self._read_metadata()
        state = self._access_explicit_version(metadata.secret_version_name)
        return VersionedXTokenState(str(metadata.revision), state)

    def acquire_refresh_lease(
        self,
        expected_revision: str,
        *,
        owner_id: str,
        now_utc: datetime,
        expires_at_utc: datetime,
    ) -> XTokenRefreshLease | None:
        revision = _parse_revision(expected_revision)
        _require_owner(owner_id)
        _require_utc(now_utc, "OAuth refresh lease acquisition time")
        _require_utc(expires_at_utc, "OAuth refresh lease expiry")
        if expires_at_utc != now_utc + self._config.lease_duration:
            raise XTokenStateError("OAuth refresh lease expiry does not match configured duration")

        def operation(transaction: _Transaction) -> bool:
            metadata = self._metadata_in_transaction(transaction)
            if metadata.revision != revision:
                return False
            if (
                metadata.refresh_lease_owner is not None
                and metadata.refresh_lease_expires_at_utc is not None
                and metadata.refresh_lease_expires_at_utc > now_utc
            ):
                return False
            transaction.update(
                self._reference,
                {
                    "refresh_lease_owner": owner_id,
                    "refresh_lease_expires_at_utc": expires_at_utc,
                },
            )
            return True

        try:
            acquired = self._transactional(operation)(self._firestore_client.transaction())
        except Exception:
            raise XTokenStoreError("OAuth refresh lease transaction failed") from None
        if not isinstance(acquired, bool):
            raise XTokenStoreError("OAuth refresh lease transaction returned an invalid result")
        return XTokenRefreshLease(expected_revision, owner_id, expires_at_utc) if acquired else None

    def release_refresh_lease(self, lease: XTokenRefreshLease) -> bool:
        _require_lease(lease)
        revision = _parse_revision(lease.expected_revision)

        def operation(transaction: _Transaction) -> bool:
            metadata = self._metadata_in_transaction(transaction)
            if metadata.revision != revision:
                return True
            if metadata.refresh_lease_owner != lease.owner_id:
                return False
            transaction.update(
                self._reference,
                {
                    "refresh_lease_owner": None,
                    "refresh_lease_expires_at_utc": None,
                },
            )
            return True

        try:
            released = self._transactional(operation)(self._firestore_client.transaction())
        except Exception:
            raise XTokenStoreError("OAuth refresh lease release transaction failed") from None
        if not isinstance(released, bool):
            raise XTokenStoreError("OAuth refresh lease release returned an invalid result")
        return released

    def replace_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool:
        """Preserve the store CAS contract; production refresh uses the leased variant."""

        return self._persist_and_transition(
            expected_revision=_parse_revision(expected_revision),
            replacement=replacement,
            lease=None,
        )

    def replace_if_revision_with_lease(
        self,
        lease: XTokenRefreshLease,
        replacement: XOAuthTokenState,
    ) -> bool:
        _require_lease(lease)
        return self._persist_and_transition(
            expected_revision=_parse_revision(lease.expected_revision),
            replacement=replacement,
            lease=lease,
        )

    def _persist_and_transition(
        self,
        *,
        expected_revision: int,
        replacement: XOAuthTokenState,
        lease: XTokenRefreshLease | None,
    ) -> bool:
        if not isinstance(replacement, XOAuthTokenState):
            raise XTokenStateError("Replacement OAuth token state is invalid")
        before = self._read_metadata()
        if before.revision != expected_revision:
            return False
        if lease is None and before.refresh_lease_owner is not None:
            return False
        if lease is not None and before.refresh_lease_owner != lease.owner_id:
            return False

        candidate_version = self._add_secret_version(replacement)
        updated_at_utc = self._clock()
        _require_utc(updated_at_utc, "OAuth token authority update time")

        def operation(transaction: _Transaction) -> bool:
            current = self._metadata_in_transaction(transaction)
            if current.revision != expected_revision:
                return False
            if lease is None:
                if current.refresh_lease_owner is not None:
                    return False
            elif (
                current.refresh_lease_owner != lease.owner_id
                or current.refresh_lease_expires_at_utc != lease.expires_at_utc
                or lease.expires_at_utc <= updated_at_utc
            ):
                return False
            transaction.update(
                self._reference,
                {
                    "revision": expected_revision + 1,
                    "secret_version_name": candidate_version,
                    "previous_secret_version_name": current.secret_version_name,
                    "updated_at_utc": updated_at_utc,
                    "refresh_lease_owner": None,
                    "refresh_lease_expires_at_utc": None,
                },
            )
            return True

        try:
            replaced = self._transactional(operation)(self._firestore_client.transaction())
        except Exception:
            return self._reconcile_uncertain_authority(
                candidate_version,
                expected_revision + 1,
                before.previous_secret_version_name,
            )
        if not isinstance(replaced, bool):
            raise XTokenStoreError("OAuth token authority transaction returned an invalid result")
        if not replaced:
            self._disable_best_effort(candidate_version)
            return False
        self._disable_best_effort(before.previous_secret_version_name)
        return True

    def _reconcile_uncertain_authority(
        self,
        candidate_version: str,
        expected_new_revision: int,
        cleanup_version: str | None,
    ) -> bool:
        try:
            current = self._read_metadata()
        except Exception:
            raise XTokenAuthorityUnconfirmedError(candidate_version) from None
        if (
            current.revision == expected_new_revision
            and current.secret_version_name == candidate_version
        ):
            self._disable_best_effort(cleanup_version)
            return True
        raise XTokenAuthorityPersistenceError(candidate_version)

    def _read_metadata(self) -> _TokenAuthorityMetadata:
        try:
            snapshot = self._reference.get()
        except Exception:
            raise XTokenStoreError("OAuth token authority metadata could not be read") from None
        if not snapshot.exists:
            raise XTokenStateError("OAuth token authority metadata is not initialized")
        return _parse_metadata(snapshot.to_dict(), self._config)

    def _metadata_in_transaction(self, transaction: _Transaction) -> _TokenAuthorityMetadata:
        snapshot = self._reference.get(transaction=transaction)
        if not snapshot.exists:
            raise XTokenStateError("OAuth token authority metadata is not initialized")
        return _parse_metadata(snapshot.to_dict(), self._config)

    def _access_explicit_version(self, version_name: str) -> XOAuthTokenState:
        self._config.validate_version_name(version_name)
        try:
            response = self._secrets.access_secret_version(request={"name": version_name})
            response_name = getattr(response, "name", None)
            payload = getattr(getattr(response, "payload", None), "data", None)
        except Exception:
            raise XTokenStoreError(
                "Authoritative OAuth token secret version could not be read"
            ) from None
        if response_name != version_name or not isinstance(payload, bytes):
            raise XTokenStateError("Secret Manager returned an invalid explicit token version")
        return deserialize_token_state(payload)

    def _add_secret_version(self, replacement: XOAuthTokenState) -> str:
        payload = serialize_token_state(replacement)
        try:
            response = self._secrets.add_secret_version(
                request={"parent": self._config.secret_name, "payload": {"data": payload}}
            )
            version_name = self._config.validate_version_name(getattr(response, "name", None))
        except XTokenStateError:
            raise
        except Exception:
            raise XTokenSecretStorageError(
                "Refreshed OAuth token generation could not be stored securely"
            ) from None
        return version_name

    def _disable_best_effort(self, version_name: str | None) -> None:
        if version_name is None:
            return
        try:
            self._config.validate_version_name(version_name)
            self._secrets.disable_secret_version(request={"name": version_name})
        except Exception:
            return


def serialize_token_state(state: XOAuthTokenState) -> bytes:
    if not isinstance(state, XOAuthTokenState):
        raise XTokenStateError("OAuth token state is invalid")
    payload = {
        "access_token": state.access_token,
        "expires_at_utc": state.expires_at_utc.isoformat().replace("+00:00", "Z"),
        "refresh_token": state.refresh_token,
        "schema_version": TOKEN_PAYLOAD_SCHEMA_VERSION,
        "scopes": list(state.scopes),
        "token_type": state.token_type,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deserialize_token_state(payload: bytes) -> XOAuthTokenState:
    if not isinstance(payload, bytes):
        raise XTokenStateError("OAuth token secret payload must be bytes")
    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise XTokenStateError("OAuth token secret payload is malformed") from None
    required = {
        "access_token",
        "expires_at_utc",
        "refresh_token",
        "schema_version",
        "scopes",
        "token_type",
    }
    if not isinstance(raw, Mapping) or set(raw) != required:
        raise XTokenStateError("OAuth token secret payload has an invalid shape")
    if raw.get("schema_version") != TOKEN_PAYLOAD_SCHEMA_VERSION:
        raise XTokenStateError("OAuth token secret payload has an unsupported schema")
    expires_at = _parse_utc_timestamp(raw.get("expires_at_utc"))
    scopes = raw.get("scopes")
    if not isinstance(scopes, list):
        raise XTokenStateError("OAuth token secret payload has invalid scopes")
    try:
        return XOAuthTokenState(
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            expires_at_utc=expires_at,
            token_type=raw["token_type"],
            scopes=tuple(scopes),
        )
    except (KeyError, TypeError, XTokenStateError):
        raise XTokenStateError("OAuth token secret payload is invalid") from None


def _parse_metadata(
    raw: Mapping[str, Any] | None,
    config: CloudXTokenStateStoreConfig,
) -> _TokenAuthorityMetadata:
    if not isinstance(raw, Mapping):
        raise XTokenStateError("OAuth token authority metadata must be an object")
    required = {
        "schema_version",
        "revision",
        "secret_version_name",
        "previous_secret_version_name",
        "updated_at_utc",
        "refresh_lease_owner",
        "refresh_lease_expires_at_utc",
    }
    if set(raw) != required or raw.get("schema_version") != TOKEN_METADATA_SCHEMA_VERSION:
        raise XTokenStateError("OAuth token authority metadata has an invalid schema")
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise XTokenStateError("OAuth token authority revision must be positive")
    version_name = config.validate_version_name(raw.get("secret_version_name"))
    previous = raw.get("previous_secret_version_name")
    if previous is not None:
        previous = config.validate_version_name(previous)
        if previous == version_name:
            raise XTokenStateError("OAuth token authority versions must be distinct")
    updated_at = raw.get("updated_at_utc")
    _require_utc(updated_at, "OAuth token authority update time")
    lease_owner = raw.get("refresh_lease_owner")
    lease_expiry = raw.get("refresh_lease_expires_at_utc")
    if (lease_owner is None) != (lease_expiry is None):
        raise XTokenStateError("OAuth refresh lease metadata is incomplete")
    if lease_owner is not None:
        _require_owner(lease_owner)
        _require_utc(lease_expiry, "OAuth refresh lease expiry")
    return _TokenAuthorityMetadata(
        revision=revision,
        secret_version_name=version_name,
        previous_secret_version_name=previous,
        updated_at_utc=updated_at,
        refresh_lease_owner=lease_owner,
        refresh_lease_expires_at_utc=lease_expiry,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise XTokenStateError("OAuth token secret expiry must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise XTokenStateError("OAuth token secret expiry is malformed") from None
    _require_utc(parsed, "OAuth token secret expiry")
    return parsed


def _parse_revision(value: str) -> int:
    if not isinstance(value, str) or USER_ID_PATTERN.fullmatch(value) is None:
        raise XTokenStateError("OAuth token-state revision must be a positive integer")
    return int(value)


def _require_lease(value: Any) -> None:
    if not isinstance(value, XTokenRefreshLease):
        raise XTokenStateError("OAuth refresh lease is invalid")


def _require_owner(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value != value.strip()
        or not value.isprintable()
    ):
        raise XTokenStateError("OAuth refresh lease owner is invalid")


def _require_utc(value: Any, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise XTokenStateError(f"{label} must be timezone-aware UTC")


def _require_pattern(value: Any, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise XTokenStateError(f"{label} is invalid")


def _default_transactional_wrapper() -> TransactionalWrapper:
    from google.cloud.firestore_v1.transaction import transactional

    return transactional


def _utc_now() -> datetime:
    return datetime.now(UTC)
