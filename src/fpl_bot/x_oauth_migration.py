"""Privileged, metadata-only legacy migration; never called by runtime consumers."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, _parse_metadata
from fpl_bot.firestore_state import FirestoreClient, TransactionalWrapper
from fpl_bot.x_errors import XTokenStateError, XTokenStoreError


@dataclass(frozen=True, slots=True)
class LegacyAuthorityExpectation:
    """Exact reviewed authority, NOT an inference from a missing/expired lease."""

    revision: int
    secret_version_name: str
    previous_secret_version_name: str | None
    updated_at_utc: datetime

    def legacy_document(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "secret_version_name": self.secret_version_name,
            "previous_secret_version_name": self.previous_secret_version_name,
            "updated_at_utc": self.updated_at_utc,
            "refresh_lease_owner": None,
            "refresh_lease_expires_at_utc": None,
        }

    def migrated_document(self, config: CloudXTokenStateStoreConfig) -> dict[str, Any]:
        document = {
            **self.legacy_document(),
            "schema_version": 2,
            "refresh_attempt_generation": 0,
            "refresh_attempt": None,
        }
        # Reuse strict runtime validation; this is the only legacy compatibility path.
        _parse_metadata(document, config)
        return document


def migrate_legacy_authority(
    config: CloudXTokenStateStoreConfig,
    expectation: LegacyAuthorityExpectation,
    *,
    firestore_client: FirestoreClient,
    consumers_quiesced: bool,
    legacy_history_reviewed: bool,
    transactional_wrapper: TransactionalWrapper | None = None,
) -> str:
    """CAS exact reviewed metadata without reading/writing secrets or contacting X.

    Both attestations require external operational evidence. Neither lease absence
    nor this function can prove a legacy provider outcome. An active/expired legacy
    lease, unexpected pointer, or previously migrated uncertainty is never repaired.
    A lost commit response is recoverable by repeating this exact expectation.
    """
    if consumers_quiesced is not True or legacy_history_reviewed is not True:
        raise XTokenStateError("Migration requires quiescence and reviewed legacy authority")
    if not isinstance(expectation, LegacyAuthorityExpectation):
        raise XTokenStateError("Legacy authority expectation is invalid")
    migrated = expectation.migrated_document(config)
    reference = firestore_client.collection(config.metadata_collection).document(
        config.metadata_document_id
    )
    if transactional_wrapper is None:
        from google.cloud.firestore_v1.transaction import transactional

        transactional_wrapper = transactional

    def operation(transaction: Any) -> str:
        snapshot = reference.get(transaction=transaction)
        raw = snapshot.to_dict() if snapshot.exists else None
        if not isinstance(raw, Mapping):
            raise XTokenStateError("Reviewed legacy authority is missing")
        # Equality alone would accept bools as integers (True == 1). Validate the
        # actual persisted types, including on replay, before comparing authority.
        schema = raw.get("schema_version")
        if type(schema) is not int or schema not in {1, 2}:
            raise XTokenStateError("Legacy authority schema is invalid")
        _parse_metadata(
            raw
            if schema == 2
            else {
                **raw,
                "schema_version": 2,
                "refresh_attempt_generation": 0,
                "refresh_attempt": None,
            },
            config,
        )
        if dict(raw) == migrated:
            return "already_migrated"
        if dict(raw) != expectation.legacy_document():
            raise XTokenStateError("Authority differs from exact reviewed migration expectation")
        transaction.update(
            reference,
            {"schema_version": 2, "refresh_attempt_generation": 0, "refresh_attempt": None},
        )
        return "migrated"

    try:
        return transactional_wrapper(operation)(firestore_client.transaction())
    except XTokenStateError:
        raise
    except Exception:
        # Commit might have succeeded. Repeating the same expectation is safe.
        raise XTokenStoreError("Migration unconfirmed; reconcile exact expectation") from None
