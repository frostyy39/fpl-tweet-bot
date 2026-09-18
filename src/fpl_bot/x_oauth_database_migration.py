"""Operator-only, metadata-only relocation. Source fencing precedes destination creation.

Firestore cannot transact across databases. A retired source is therefore permanent:
after a lost acknowledgement, rerun the exact reviewed migration, never unretire it.
Runtime schema-2 readers reject the tombstone before accessing Secret Manager or X.
Quiescence must cover old revisions and all consumers, not merely the refresh lease.
"""

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, _parse_metadata
from fpl_bot.x_errors import XTokenStateError, XTokenStoreError


def _safe_authority(raw: Any, config: CloudXTokenStateStoreConfig) -> dict[str, Any]:
    parsed = _parse_metadata(raw, config)
    if parsed.refresh_lease_owner is not None or (
        parsed.refresh_attempt is not None
        and parsed.refresh_attempt.state.value
        not in {"committed", "operator_reauthorized", "aborted_before_dispatch"}
    ):
        raise XTokenStateError("OAuth authority is not quiescent and reusable")
    return dict(raw)


def authority_digest(raw: Mapping[str, Any], config: CloudXTokenStateStoreConfig) -> str:
    safe = _safe_authority(raw, config)

    def timestamp(value: Any) -> str:
        if not isinstance(value, datetime):
            raise XTokenStateError("Invalid metadata value")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=timestamp)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def relocate_authority(
    config: CloudXTokenStateStoreConfig,
    *,
    source_client: Any,
    destination_client: Any,
    source_database: str,
    destination_database: str,
    expected_digest: str,
    expected_revision: int,
    now: datetime,
    consumers_quiesced: bool,
    transactional_wrapper: Any = None,
) -> str:
    """Two ordered CAS transactions; no secret/provider call, no rollback to source.

    Before fence: source remains sole authority. After fence: zero authorities until
    destination commit. After destination commit: destination is the sole authority.
    Lost commits are recovered by exact replay. Conflicting state is never replaced.
    """
    if (
        consumers_quiesced is not True
        or source_database == destination_database
        or destination_database == "(default)"
        or not re.fullmatch(r"[a-z][a-z0-9-]{2,61}[a-z0-9]", destination_database)
        or not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        or type(expected_revision) is not int
        or expected_revision <= 0
        or not isinstance(now, datetime)
        or now.utcoffset() is None
        or now.utcoffset().total_seconds() != 0
    ):
        raise XTokenStateError("Relocation requires exact reviewed authority and quiescence")
    if transactional_wrapper is None:
        from google.cloud.firestore_v1.transaction import transactional

        transactional_wrapper = transactional
    source = source_client.collection(config.metadata_collection).document(
        config.metadata_document_id
    )
    destination = destination_client.collection(config.metadata_collection).document(
        config.metadata_document_id
    )
    identity = hashlib.sha256(
        f"{source_database}/{destination_database}/{expected_digest}".encode()
    ).hexdigest()

    def reviewed(raw: Any) -> dict[str, Any]:
        authority = _safe_authority(raw, config)
        if (
            authority["revision"] != expected_revision
            or authority_digest(authority, config) != expected_digest
        ):
            raise XTokenStateError("Source authority differs from reviewed expectation")
        return authority

    def retired(raw: Any) -> dict[str, Any]:
        if (
            not isinstance(raw, Mapping)
            or set(raw)
            != {
                "schema_version",
                "status",
                "migration_id",
                "source_database",
                "destination_database",
                "retired_at_utc",
                "retired_authority",
            }
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 3
            or raw["status"] != "retired"
            or raw["migration_id"] != identity
            or raw["source_database"] != source_database
            or raw["destination_database"] != destination_database
            or not isinstance(raw["retired_at_utc"], datetime)
            or raw["retired_at_utc"].utcoffset() is None
            or raw["retired_at_utc"].utcoffset().total_seconds() != 0
        ):
            raise XTokenStateError("Source retirement differs from reviewed relocation")
        return reviewed(raw["retired_authority"])

    def fence(transaction: Any) -> dict[str, Any]:
        snapshot = source.get(transaction=transaction)
        raw = snapshot.to_dict() if snapshot.exists else None
        if isinstance(raw, Mapping) and raw.get("status") == "retired":
            return retired(raw)
        authority = reviewed(raw)
        transaction.set(
            source,
            {
                "schema_version": 3,
                "status": "retired",
                "migration_id": identity,
                "source_database": source_database,
                "destination_database": destination_database,
                "retired_at_utc": now,
                "retired_authority": authority,
            },
        )
        return authority

    try:
        # A destination existing before source fencing is not an acceptable migration.
        existing_destination = destination.get().exists
        existing_source = source.get().to_dict()
        if existing_destination and not (
            isinstance(existing_source, Mapping) and existing_source.get("status") == "retired"
        ):
            raise XTokenStateError("Destination already exists before source retirement")
        authority = transactional_wrapper(fence)(source_client.transaction())
        retired(source.get().to_dict())

        def establish(transaction: Any) -> str:
            snapshot = destination.get(transaction=transaction)
            if snapshot.exists:
                raw = snapshot.to_dict()
                reviewed(raw)
                if raw != authority:
                    raise XTokenStateError("Destination conflicts with reviewed authority")
                return "already_relocated"
            transaction.create(destination, authority)
            return "relocated"

        return transactional_wrapper(establish)(destination_client.transaction())
    except XTokenStateError:
        raise
    except Exception:
        raise XTokenStoreError(
            "Relocation unconfirmed; keep quiesced and reconcile exact migration"
        ) from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--destination-database", required=True)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--expected-digest")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--consumers-quiesced", action="store_true")
    args = parser.parse_args()
    try:
        from google.cloud import firestore

        config = CloudXTokenStateStoreConfig(args.project, args.secret_id, args.expected_user_id)
        source = firestore.Client(project=args.project, database=args.source_database)
        if args.inspect:
            raw = (
                source.collection(config.metadata_collection)
                .document(config.metadata_document_id)
                .get()
                .to_dict()
            )
            digest = authority_digest(raw, config)
            print(
                json.dumps(
                    {
                        "revision": raw["revision"],
                        "digest": digest,
                        "secret_version_name": raw["secret_version_name"],
                    }
                )
            )
            return 0
        result = relocate_authority(
            config,
            source_client=source,
            destination_client=firestore.Client(
                project=args.project, database=args.destination_database
            ),
            source_database=args.source_database,
            destination_database=args.destination_database,
            expected_digest=args.expected_digest,
            expected_revision=args.expected_revision,
            now=datetime.now(UTC),
            consumers_quiesced=args.consumers_quiesced,
        )
        print(json.dumps({"result": result, "revision": args.expected_revision}))
        return 0
    except Exception:
        print(
            json.dumps({"result": "relocation_unconfirmed", "action": "keep_consumers_quiesced"}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
