"""Distributed X OAuth token state using Firestore authority and Secret Manager data."""

import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fpl_bot.x_token_bootstrap import ValidatedLocalTokenState

from fpl_bot.firestore_state import FirestoreClient, TransactionalWrapper
from fpl_bot.x_errors import (
    XTokenAuthorityPersistenceError,
    XTokenAuthorityUnconfirmedError,
    XTokenBootstrapReconciliationError,
    XTokenConcurrencyError,
    XTokenRefreshUncertainError,
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
TOKEN_METADATA_SCHEMA_VERSION = 2
DEFAULT_TOKEN_METADATA_COLLECTION = "x_oauth_token_authority"
DEFAULT_REFRESH_LEASE_DURATION = timedelta(minutes=1)

PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}\Z")
SECRET_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
COLLECTION_ID_PATTERN = re.compile(r"[^/]{1,1500}\Z")
USER_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")
PROJECT_NUMBER_PATTERN = USER_ID_PATTERN


class _Snapshot(Protocol):
    exists: bool

    def to_dict(self) -> Mapping[str, Any] | None: ...


class _DocumentReference(Protocol):
    def get(self, *, transaction: Any | None = None) -> _Snapshot: ...


class _CollectionReference(Protocol):
    def document(self, document_id: str) -> _DocumentReference: ...


class _Transaction(Protocol):
    def create(self, reference: _DocumentReference, data: Mapping[str, Any]) -> None: ...

    def update(self, reference: _DocumentReference, fields: Mapping[str, Any]) -> None: ...


class SecretManagerClient(Protocol):
    def access_secret_version(self, request: Mapping[str, Any]) -> Any: ...

    def add_secret_version(self, request: Mapping[str, Any]) -> Any: ...

    def disable_secret_version(self, request: Mapping[str, Any]) -> Any: ...

    def list_secret_versions(self, request: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class CloudXTokenStateStoreConfig:
    """Validated non-secret resource identity for one configured X account."""

    project_id: str
    secret_id: str
    expected_user_id: str
    project_number: str | None = None
    metadata_collection: str = DEFAULT_TOKEN_METADATA_COLLECTION
    lease_duration: timedelta = DEFAULT_REFRESH_LEASE_DURATION

    def __post_init__(self) -> None:
        _require_pattern(self.project_id, PROJECT_ID_PATTERN, "GCP project ID")
        _require_pattern(self.secret_id, SECRET_ID_PATTERN, "X token secret ID")
        _require_pattern(self.expected_user_id, USER_ID_PATTERN, "expected X user ID")
        if self.project_number is not None:
            _require_pattern(self.project_number, PROJECT_NUMBER_PATTERN, "GCP project number")
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

    def canonicalize_api_version_name(self, value: Any) -> str:
        pattern = re.compile(
            rf"projects/(?P<project>[A-Za-z0-9.-]+)/secrets/"
            rf"{re.escape(self.secret_id)}/versions/(?P<version>[1-9][0-9]*)\Z"
        )
        match = pattern.fullmatch(value) if isinstance(value, str) else None
        expected_projects = {self.project_id}
        if self.project_number is not None:
            expected_projects.add(self.project_number)
        if match is None or match.group("project") not in expected_projects:
            raise XTokenStateError("Secret Manager returned an invalid token version name")
        return f"{self.secret_name}/versions/{match.group('version')}"


@dataclass(frozen=True, slots=True)
class _TokenAuthorityMetadata:
    revision: int
    secret_version_name: str
    previous_secret_version_name: str | None
    updated_at_utc: datetime
    refresh_lease_owner: str | None
    refresh_lease_expires_at_utc: datetime | None
    refresh_attempt_generation: int
    refresh_attempt: "RefreshAttempt | None"


class RefreshAttemptState(StrEnum):
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    PERSISTING = "persisting"
    UNCERTAIN = "uncertain"
    COMMITTED = "committed"
    RECOVERED = "operator_reauthorized"
    ABORTED = "aborted_before_dispatch"


@dataclass(frozen=True, slots=True)
class RefreshAttempt:
    """Non-secret evidence; identity/revision/generation never change within an attempt."""

    attempt_id: str
    generation: int
    credential_revision: int
    state: RefreshAttemptState
    claimed_at_utc: datetime
    updated_at_utc: datetime
    dispatched_at_utc: datetime | None = None
    candidate_version_name: str | None = None
    classification: str | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "generation": self.generation,
            "credential_revision": self.credential_revision,
            "state": self.state.value,
            "claimed_at_utc": self.claimed_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "dispatched_at_utc": self.dispatched_at_utc,
            "candidate_version_name": self.candidate_version_name,
            "classification": self.classification,
        }


class InitialTokenStateStatus(StrEnum):
    INITIALIZED = "initialized"
    ALREADY_INITIALIZED = "already_initialized"


@dataclass(frozen=True, slots=True)
class InitialTokenStateResult:
    status: InitialTokenStateStatus
    revision: str
    secret_version_name: str


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
        attempt = metadata.refresh_attempt
        if attempt is not None:
            if attempt.state is RefreshAttemptState.UNCERTAIN:
                raise XTokenRefreshUncertainError("OAuth refresh authority requires recovery")
            if attempt.state in {RefreshAttemptState.DISPATCHED, RefreshAttemptState.PERSISTING}:
                if metadata.refresh_lease_expires_at_utc <= self._clock():
                    self.mark_refresh_uncertain(_metadata_lease(metadata))
                    raise XTokenRefreshUncertainError("Abandoned OAuth refresh requires recovery")
                raise XTokenConcurrencyError("OAuth refresh is already in progress")
            if (
                attempt.state is RefreshAttemptState.CLAIMED
                and metadata.refresh_lease_expires_at_utc > self._clock()
            ):
                raise XTokenConcurrencyError("OAuth refresh is already claimed")
        state = self._access_explicit_version(metadata.secret_version_name)
        return VersionedXTokenState(str(metadata.revision), state)

    def initialize(self, initial_state: XOAuthTokenState) -> InitialTokenStateResult:
        """Create the first secret generation and authority document exactly once."""

        if not isinstance(initial_state, XOAuthTokenState):
            raise XTokenStateError("Initial OAuth token state is invalid")
        existing = self._authority_snapshot()
        if existing.exists:
            metadata = _parse_metadata(existing.to_dict(), self._config)
            self._access_explicit_version(metadata.secret_version_name)
            return InitialTokenStateResult(
                InitialTokenStateStatus.ALREADY_INITIALIZED,
                str(metadata.revision),
                metadata.secret_version_name,
            )
        existing_versions = self._existing_secret_version_names()
        candidate_version = (
            self._matching_initial_candidate(initial_state, existing_versions)
            if existing_versions
            else self._add_initial_secret_version(initial_state)
        )
        updated_at_utc = self._clock()
        _require_utc(updated_at_utc, "OAuth token authority initialization time")
        document = {
            "schema_version": TOKEN_METADATA_SCHEMA_VERSION,
            "revision": 1,
            "secret_version_name": candidate_version,
            "previous_secret_version_name": None,
            "updated_at_utc": updated_at_utc,
            "refresh_lease_owner": None,
            "refresh_lease_expires_at_utc": None,
            "refresh_attempt_generation": 0,
            "refresh_attempt": None,
        }

        def operation(transaction: _Transaction) -> bool:
            snapshot = self._reference.get(transaction=transaction)
            if snapshot.exists:
                return False
            transaction.create(self._reference, document)
            return True

        try:
            initialized = self._transactional(operation)(self._firestore_client.transaction())
        except Exception:
            return self._reconcile_initialization(candidate_version)
        if not isinstance(initialized, bool):
            raise XTokenAuthorityUnconfirmedError(candidate_version)
        if not initialized:
            return self._reconcile_initialization(candidate_version)
        return InitialTokenStateResult(
            InitialTokenStateStatus.INITIALIZED,
            "1",
            candidate_version,
        )

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
            attempt = metadata.refresh_attempt
            if attempt is not None:
                if attempt.state in {
                    RefreshAttemptState.DISPATCHED,
                    RefreshAttemptState.PERSISTING,
                    RefreshAttemptState.UNCERTAIN,
                }:
                    if (
                        attempt.state is not RefreshAttemptState.UNCERTAIN
                        and metadata.refresh_lease_expires_at_utc <= now_utc
                    ):
                        document = attempt.to_document()
                        document.update(
                            state=RefreshAttemptState.UNCERTAIN.value,
                            updated_at_utc=now_utc,
                            classification="outcome_unknown",
                        )
                        transaction.update(self._reference, {"refresh_attempt": document})
                    return False
                if attempt.attempt_id == owner_id:
                    return False  # An old attempt identity cannot authorize a fresh dispatch.
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
                    "refresh_attempt_generation": metadata.refresh_attempt_generation + 1,
                    "refresh_attempt": RefreshAttempt(
                        owner_id,
                        metadata.refresh_attempt_generation + 1,
                        revision,
                        RefreshAttemptState.CLAIMED,
                        now_utc,
                        now_utc,
                    ).to_document(),
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

    def begin_refresh_dispatch(self, lease: XTokenRefreshLease) -> bool:
        """Commit the irreversible no-reuse barrier BEFORE entering the HTTP client."""
        return self._transition_refresh(
            lease,
            {RefreshAttemptState.CLAIMED},
            RefreshAttemptState.DISPATCHED,
            require_live_lease=True,
        )

    def mark_refresh_uncertain(self, lease: XTokenRefreshLease) -> bool:
        return self._transition_refresh(
            lease,
            {
                RefreshAttemptState.DISPATCHED,
                RefreshAttemptState.PERSISTING,
                RefreshAttemptState.UNCERTAIN,
            },
            RefreshAttemptState.UNCERTAIN,
            classification="outcome_unknown",
        )

    def _transition_refresh(
        self,
        lease: XTokenRefreshLease,
        allowed: set[RefreshAttemptState],
        state: RefreshAttemptState,
        *,
        require_live_lease: bool = False,
        classification: str | None = None,
        candidate_version: str | None = None,
    ) -> bool:
        _require_lease(lease)
        now = self._clock()
        _require_utc(now, "OAuth refresh transition time")

        def operation(transaction: _Transaction) -> bool:
            metadata = self._metadata_in_transaction(transaction)
            attempt = metadata.refresh_attempt
            if (
                not _owns_attempt(metadata, lease)
                or attempt.state not in allowed
                or (require_live_lease and lease.expires_at_utc <= now)
            ):
                return False
            document = attempt.to_document()
            document.update(state=state.value, updated_at_utc=now, classification=classification)
            if state is RefreshAttemptState.DISPATCHED:
                document["dispatched_at_utc"] = now
            if candidate_version is not None:
                self._config.validate_version_name(candidate_version)
                if attempt.candidate_version_name is not None:
                    return False
                document["candidate_version_name"] = candidate_version
            transaction.update(self._reference, {"refresh_attempt": document})
            return True

        try:
            result = self._transactional(operation)(self._firestore_client.transaction())
        except Exception:
            # Never proceed to HTTP on an unknown dispatch-commit outcome.
            raise XTokenStoreError("OAuth refresh transition could not be confirmed") from None
        if not isinstance(result, bool):
            raise XTokenStoreError("OAuth refresh transition returned an invalid result")
        return result

    def release_refresh_lease(self, lease: XTokenRefreshLease) -> bool:
        _require_lease(lease)
        revision = _parse_revision(lease.expected_revision)

        def operation(transaction: _Transaction) -> bool:
            metadata = self._metadata_in_transaction(transaction)
            if metadata.revision != revision:
                return True
            if metadata.refresh_lease_owner != lease.owner_id:
                return False
            if not _owns_attempt(metadata, lease):
                return False
            if metadata.refresh_attempt.state is not RefreshAttemptState.CLAIMED:
                return False  # Expiry/release is NEVER evidence of failed provider rotation.
            attempt = metadata.refresh_attempt.to_document()
            attempt.update(
                state=RefreshAttemptState.ABORTED.value,
                updated_at_utc=self._clock(),
                classification="pre_dispatch_aborted",
            )
            transaction.update(
                self._reference,
                {
                    "refresh_lease_owner": None,
                    "refresh_lease_expires_at_utc": None,
                    "refresh_attempt": attempt,
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

    def reseed_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool:
        """CAS one reviewed admin generation, reconciling a unique orphan first."""

        revision = _parse_revision(expected_revision)
        if not isinstance(replacement, XOAuthTokenState):
            raise XTokenStateError("Replacement OAuth token state is invalid")
        before = self._read_metadata()
        if before.revision != revision or before.refresh_lease_owner is not None:
            return False
        candidate = self._matching_reseed_candidate(replacement, before)
        if candidate is None:
            try:
                candidate = self._add_secret_version(replacement)
            except XTokenSecretStorageError:
                candidate = self._matching_reseed_candidate(replacement, before)
                if candidate is None:
                    raise
        return self._persist_and_transition(
            expected_revision=revision,
            replacement=replacement,
            lease=None,
            candidate_version=candidate,
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
        candidate_version: str | None = None,
        recovery_attempt_id: str | None = None,
    ) -> bool:
        if not isinstance(replacement, XOAuthTokenState):
            raise XTokenStateError("Replacement OAuth token state is invalid")
        before = self._read_metadata()
        if before.revision != expected_revision:
            return False
        if recovery_attempt_id is not None and not _recoverable_attempt(
            before, recovery_attempt_id
        ):
            return False
        if lease is None and before.refresh_lease_owner is not None and recovery_attempt_id is None:
            return False
        if lease is not None and before.refresh_lease_owner != lease.owner_id:
            return False

        if lease is not None:
            if not _owns_attempt(before, lease):
                return False
            if candidate_version is None:
                if not self._transition_refresh(
                    lease,
                    {RefreshAttemptState.DISPATCHED},
                    RefreshAttemptState.PERSISTING,
                ):
                    return False
            elif before.refresh_attempt.candidate_version_name != candidate_version:
                return False

        if candidate_version is None:
            candidate_version = self._add_secret_version(replacement)
            if lease is not None and not self._transition_refresh(
                lease,
                {RefreshAttemptState.PERSISTING},
                RefreshAttemptState.PERSISTING,
                candidate_version=candidate_version,
            ):
                raise XTokenAuthorityUnconfirmedError(candidate_version)
        else:
            self._config.validate_version_name(candidate_version)
        updated_at_utc = self._clock()
        _require_utc(updated_at_utc, "OAuth token authority update time")

        def operation(transaction: _Transaction) -> bool:
            current = self._metadata_in_transaction(transaction)
            if current.revision != expected_revision:
                return False
            if lease is None:
                if recovery_attempt_id is not None:
                    if not _recoverable_attempt(current, recovery_attempt_id):
                        return False
                elif current.refresh_lease_owner is not None:
                    return False
            elif (
                not _owns_attempt(current, lease)
                or current.refresh_attempt.state
                not in {
                    RefreshAttemptState.PERSISTING,
                    RefreshAttemptState.UNCERTAIN,
                }
                or current.refresh_attempt.candidate_version_name != candidate_version
            ):
                return False
            attempt = current.refresh_attempt if lease is not None or recovery_attempt_id else None
            attempt_document = None if attempt is None else attempt.to_document()
            if attempt_document is not None:
                attempt_document.update(
                    state=(
                        RefreshAttemptState.RECOVERED
                        if recovery_attempt_id
                        else RefreshAttemptState.COMMITTED
                    ).value,
                    candidate_version_name=candidate_version,
                    updated_at_utc=updated_at_utc,
                    classification="operator_reauthorized" if recovery_attempt_id else None,
                )
            transaction.update(
                self._reference,
                {
                    "revision": expected_revision + 1,
                    "secret_version_name": candidate_version,
                    "previous_secret_version_name": current.secret_version_name,
                    "updated_at_utc": updated_at_utc,
                    "refresh_lease_owner": None,
                    "refresh_lease_expires_at_utc": None,
                    "refresh_attempt": attempt_document,
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
            try:
                current = self._read_metadata()
            except Exception:
                raise XTokenAuthorityUnconfirmedError(candidate_version) from None
            if (
                current.revision == expected_revision + 1
                and current.secret_version_name == candidate_version
            ):
                self._disable_best_effort(before.previous_secret_version_name)
                return True
            self._disable_best_effort(candidate_version)
            return False
        self._disable_best_effort(before.previous_secret_version_name)
        return True

    def reconcile_refresh_attempt(self) -> bool:
        """Promote ONLY a durably bound exact candidate; never call X or retry R0."""
        metadata = self._read_metadata()
        attempt = metadata.refresh_attempt
        if attempt is None or attempt.state in {
            RefreshAttemptState.COMMITTED,
            RefreshAttemptState.RECOVERED,
        }:
            return True
        if (
            attempt.state not in {RefreshAttemptState.PERSISTING, RefreshAttemptState.UNCERTAIN}
            or attempt.candidate_version_name is None
        ):
            raise XTokenRefreshUncertainError("OAuth refresh has no proven replacement version")
        replacement = self._access_explicit_version(attempt.candidate_version_name)
        return self._persist_and_transition(
            expected_revision=metadata.revision,
            replacement=replacement,
            lease=_metadata_lease(metadata),
            candidate_version=attempt.candidate_version_name,
        )

    def recover_uncertain_if_revision(
        self,
        expected_revision: str,
        expected_attempt_id: str,
        replacement: "ValidatedLocalTokenState",
        *,
        consumers_quiesced: bool,
    ) -> bool:
        """Privileged operator-only recovery, NOT a worker/runtime or HTTP endpoint.

        Caller must quiesce all consumers and obtain/verify a fresh manual grant first.
        This explicit attestation is not a substitute for deployment/IAM controls.
        """
        from fpl_bot.x_token_bootstrap import ValidatedLocalTokenState

        if (
            consumers_quiesced is not True
            or not isinstance(replacement, ValidatedLocalTokenState)
            or replacement.x_user_id != self._config.expected_user_id
            or not replacement.state.is_valid_beyond(self._clock(), timedelta(minutes=5))
        ):
            raise XTokenStateError("OAuth recovery requires reviewed fresh manual authorization")
        revision = _parse_revision(expected_revision)
        before = self._read_metadata()
        if before.revision != revision or not _recoverable_attempt(before, expected_attempt_id):
            return False
        previous = self._access_explicit_version(before.secret_version_name)
        if secrets.compare_digest(
            previous.refresh_token.encode(),
            replacement.state.refresh_token.encode(),
        ):
            raise XTokenStateError("OAuth recovery cannot restore an uncertain old refresh token")
        return self._persist_and_transition(
            expected_revision=revision,
            replacement=replacement.state,
            lease=None,
            recovery_attempt_id=expected_attempt_id,
        )

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
        snapshot = self._authority_snapshot()
        if not snapshot.exists:
            raise XTokenStateError("OAuth token authority metadata is not initialized")
        return _parse_metadata(snapshot.to_dict(), self._config)

    def _authority_snapshot(self) -> _Snapshot:
        try:
            return self._reference.get()
        except Exception:
            raise XTokenStoreError("OAuth token authority metadata could not be read") from None

    def _existing_secret_version_names(self) -> tuple[str, ...]:
        try:
            versions = self._secrets.list_secret_versions(
                request={"parent": self._config.secret_name}
            )
            names = tuple(
                self._config.canonicalize_api_version_name(getattr(item, "name", None))
                for item in versions
            )
        except XTokenStateError:
            raise
        except Exception:
            raise XTokenStoreError("OAuth token secret versions could not be listed") from None
        return names

    def _add_initial_secret_version(self, initial_state: XOAuthTokenState) -> str:
        try:
            return self._add_secret_version(initial_state)
        except XTokenSecretStorageError:
            versions = self._existing_secret_version_names()
            if not versions:
                raise
            return self._matching_initial_candidate(initial_state, versions)

    def _matching_initial_candidate(
        self,
        initial_state: XOAuthTokenState,
        versions: tuple[str, ...],
    ) -> str:
        if len(versions) != 1:
            raise XTokenBootstrapReconciliationError(
                "OAuth token bootstrap could not identify one candidate secret version"
            )
        candidate = versions[0]
        try:
            stored = self._access_explicit_version(candidate)
        except Exception:
            raise XTokenBootstrapReconciliationError(
                "OAuth token bootstrap candidate could not be verified"
            ) from None
        if not secrets.compare_digest(
            serialize_token_state(stored),
            serialize_token_state(initial_state),
        ):
            raise XTokenBootstrapReconciliationError(
                "OAuth token bootstrap candidate does not match validated local state"
            )
        return candidate

    def _matching_reseed_candidate(
        self,
        replacement: XOAuthTokenState,
        authority: _TokenAuthorityMetadata,
    ) -> str | None:
        try:
            versions = self._secrets.list_secret_versions(
                request={"parent": self._config.secret_name}
            )
            candidates: list[str] = []
            excluded = {
                authority.secret_version_name,
                authority.previous_secret_version_name,
            }
            for item in versions:
                if not _secret_version_is_enabled(item):
                    continue
                name = self._config.canonicalize_api_version_name(getattr(item, "name", None))
                if name in excluded:
                    continue
                stored = self._access_explicit_version(name)
                if secrets.compare_digest(
                    serialize_token_state(stored),
                    serialize_token_state(replacement),
                ):
                    candidates.append(name)
        except XTokenStateError:
            raise
        except Exception:
            raise XTokenStoreError(
                "OAuth token reseed candidates could not be reconciled"
            ) from None
        if len(candidates) > 1:
            raise XTokenStoreError(
                "OAuth token reseed found multiple matching candidate generations"
            )
        return candidates[0] if candidates else None

    def _reconcile_initialization(self, candidate_version: str) -> InitialTokenStateResult:
        try:
            snapshot = self._authority_snapshot()
        except XTokenStoreError:
            raise XTokenAuthorityUnconfirmedError(candidate_version) from None
        if not snapshot.exists:
            raise XTokenAuthorityPersistenceError(candidate_version)
        metadata = _parse_metadata(snapshot.to_dict(), self._config)
        if (
            metadata.revision == 1
            and metadata.secret_version_name == candidate_version
            and metadata.previous_secret_version_name is None
            and metadata.refresh_lease_owner is None
            and metadata.refresh_lease_expires_at_utc is None
        ):
            return InitialTokenStateResult(
                InitialTokenStateStatus.INITIALIZED,
                "1",
                candidate_version,
            )
        raise XTokenAuthorityPersistenceError(candidate_version)

    def _metadata_in_transaction(self, transaction: _Transaction) -> _TokenAuthorityMetadata:
        snapshot = self._reference.get(transaction=transaction)
        if not snapshot.exists:
            raise XTokenStateError("OAuth token authority metadata is not initialized")
        return _parse_metadata(snapshot.to_dict(), self._config)

    def _access_explicit_version(self, version_name: str) -> XOAuthTokenState:
        self._config.validate_version_name(version_name)
        try:
            response = self._secrets.access_secret_version(request={"name": version_name})
            response_name = self._config.canonicalize_api_version_name(
                getattr(response, "name", None)
            )
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
            version_name = self._config.canonicalize_api_version_name(
                getattr(response, "name", None)
            )
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
        "refresh_attempt_generation",
        "refresh_attempt",
    }
    if set(raw) != required or raw.get("schema_version") != TOKEN_METADATA_SCHEMA_VERSION:
        raise XTokenStateError("OAuth authority schema requires reviewed coordinated migration")
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
    generation = raw.get("refresh_attempt_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise XTokenStateError("OAuth refresh attempt generation is invalid")
    attempt = _parse_attempt(raw.get("refresh_attempt"), config)
    if attempt is None:
        if lease_owner is not None:
            raise XTokenStateError("OAuth lease lacks durable refresh attempt evidence")
    else:
        if attempt.generation != generation or attempt.credential_revision > revision:
            raise XTokenStateError("OAuth refresh attempt generation/revision is inconsistent")
        active = attempt.state not in {
            RefreshAttemptState.ABORTED,
            RefreshAttemptState.COMMITTED,
            RefreshAttemptState.RECOVERED,
        }
        if active:
            if lease_owner != attempt.attempt_id or attempt.credential_revision != revision:
                raise XTokenStateError("OAuth active refresh attempt does not own authority")
            if attempt.candidate_version_name == version_name:
                raise XTokenStateError("OAuth replacement cannot reuse the old secret version")
        elif lease_owner is not None:
            raise XTokenStateError("OAuth terminal refresh attempt retains a lease")
        if attempt.state in {RefreshAttemptState.COMMITTED, RefreshAttemptState.RECOVERED} and (
            attempt.candidate_version_name != version_name
            or attempt.credential_revision + 1 != revision
        ):
            raise XTokenStateError("OAuth committed refresh has inconsistent authority")
    return _TokenAuthorityMetadata(
        revision=revision,
        secret_version_name=version_name,
        previous_secret_version_name=previous,
        updated_at_utc=updated_at,
        refresh_lease_owner=lease_owner,
        refresh_lease_expires_at_utc=lease_expiry,
        refresh_attempt_generation=generation,
        refresh_attempt=attempt,
    )


def _parse_attempt(raw: Any, config: CloudXTokenStateStoreConfig) -> RefreshAttempt | None:
    if raw is None:
        return None
    fields = set(RefreshAttempt.__dataclass_fields__)
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise XTokenStateError("OAuth refresh attempt has an invalid shape")
    _require_owner(raw["attempt_id"])
    for field in ("generation", "credential_revision"):
        if isinstance(raw[field], bool) or not isinstance(raw[field], int) or raw[field] <= 0:
            raise XTokenStateError("OAuth refresh attempt identity is invalid")
    try:
        state = RefreshAttemptState(raw["state"])
    except (ValueError, TypeError):
        raise XTokenStateError("OAuth refresh attempt state is invalid") from None
    claimed, updated, dispatched = (
        raw["claimed_at_utc"],
        raw["updated_at_utc"],
        raw["dispatched_at_utc"],
    )
    _require_utc(claimed, "OAuth refresh claim time")
    _require_utc(updated, "OAuth refresh update time")
    if updated < claimed:
        raise XTokenStateError("OAuth refresh attempt chronology is invalid")
    pre_dispatch = state in {RefreshAttemptState.CLAIMED, RefreshAttemptState.ABORTED}
    if pre_dispatch != (dispatched is None):
        raise XTokenStateError("OAuth refresh attempt dispatch evidence is inconsistent")
    if dispatched is not None:
        _require_utc(dispatched, "OAuth refresh dispatch time")
        if not claimed <= dispatched <= updated:
            raise XTokenStateError("OAuth refresh attempt dispatch chronology is invalid")
    candidate = raw["candidate_version_name"]
    if candidate is not None:
        config.validate_version_name(candidate)
        if state not in {
            RefreshAttemptState.PERSISTING,
            RefreshAttemptState.UNCERTAIN,
            RefreshAttemptState.COMMITTED,
            RefreshAttemptState.RECOVERED,
        }:
            raise XTokenStateError("OAuth replacement lacks successful-response evidence")
    if (
        state in {RefreshAttemptState.COMMITTED, RefreshAttemptState.RECOVERED}
        and candidate is None
    ):
        raise XTokenStateError("OAuth committed refresh lacks replacement evidence")
    classification = raw["classification"]
    expected_classification = {
        RefreshAttemptState.UNCERTAIN: "outcome_unknown",
        RefreshAttemptState.ABORTED: "pre_dispatch_aborted",
        RefreshAttemptState.RECOVERED: "operator_reauthorized",
    }.get(state)
    if classification != expected_classification:
        raise XTokenStateError("OAuth refresh attempt classification is invalid")
    return RefreshAttempt(**{**raw, "state": state})


def _owns_attempt(metadata: _TokenAuthorityMetadata, lease: XTokenRefreshLease) -> bool:
    return (
        metadata.revision == _parse_revision(lease.expected_revision)
        and metadata.refresh_lease_owner == lease.owner_id
        and metadata.refresh_lease_expires_at_utc == lease.expires_at_utc
        and metadata.refresh_attempt is not None
        and metadata.refresh_attempt.attempt_id == lease.owner_id
    )


def _metadata_lease(metadata: _TokenAuthorityMetadata) -> XTokenRefreshLease:
    return XTokenRefreshLease(
        str(metadata.revision),
        metadata.refresh_lease_owner,
        metadata.refresh_lease_expires_at_utc,
    )


def _recoverable_attempt(metadata: _TokenAuthorityMetadata, attempt_id: str) -> bool:
    return (
        metadata.refresh_attempt is not None
        and metadata.refresh_attempt.attempt_id == attempt_id
        and metadata.refresh_attempt.state is RefreshAttemptState.UNCERTAIN
    )


def _secret_version_is_enabled(value: Any) -> bool:
    state = getattr(value, "state", None)
    if state is None:
        return True
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name == "ENABLED"
    return state == 1


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
