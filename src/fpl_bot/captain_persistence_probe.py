"""Captain-only non-postable persistence smoke test. No generation or task creation."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from google.api_core.exceptions import PermissionDenied
from google.cloud import firestore, firestore_admin_v1

from fpl_bot.captain_firestore import FirestoreCaptainRepository, FirestoreCaptainVmOperations
from fpl_bot.captain_handoff import AuthenticationStatus
from fpl_bot.captain_serialization import from_document, to_document
from fpl_bot.captain_state import PostKey, SessionHealthEvidence
from fpl_bot.captain_vm_operations import VmAction

PROJECT = "fpl-frosty-bot-v1"
DATABASE = "captain-state"
NAME = f"projects/{PROJECT}/databases/{DATABASE}"


def run_probe(client, admin, now, *, transactional=firestore.transactional):
    if client.project != PROJECT or client._database_string != NAME:
        raise ValueError("isolated Captain database required")
    admin.get_database(request={"name": NAME}, retry=None, timeout=10)
    try:
        # Metadata only: no Good Luck document/state is ever requested.
        admin.get_database(
            request={"name": f"projects/{PROJECT}/databases/(default)"}, retry=None, timeout=10
        )
    except PermissionDenied:
        pass
    else:
        raise ValueError("Captain database isolation failed")

    options = {
        "project": PROJECT,
        "database": DATABASE,
        "client": client,
        "transactional_wrapper": transactional,
    }
    repository = FirestoreCaptainRepository(**options)
    operations = FirestoreCaptainVmOperations(**options)
    repository.current(PostKey("1", 900001))
    repository.posting(PostKey("1", 900001))
    repository.vm_use()
    pending = repository.pending_intents()
    operations.get(UUID(int=900001), VmAction.START)

    record = (
        now,
        Decimal("7.00"),
        "João",
        SessionHealthEvidence(now, AuthenticationStatus.NOT_CONFIRMED),
    )
    encoded = to_document(record)
    digest = hashlib.sha256(
        json.dumps(encoded, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    ref = client.collection("captain_integration_probes").document(digest)
    payload = {
        "classification": "non_postable_persistence_probe",
        "postable": False,
        "entry": encoded,
    }

    @transactional
    def persist(tx):
        snapshot = ref.get(transaction=tx)
        if snapshot.exists:
            if snapshot.to_dict() != payload:
                raise ValueError("immutable persistence probe conflict")
        else:
            tx.set(ref, payload)

    persist(client.transaction())
    persist(client.transaction())
    if from_document(ref.get().to_dict()["entry"]) != record:
        raise ValueError("persistence round-trip failed")
    return {
        "classification": "non_postable_persistence_probe",
        "postable": False,
        "database": DATABASE,
        "default_database_metadata_denied": True,
        "adapter_transaction_reads": True,
        "serialized_transaction_replay": True,
        "digest": digest,
        "pending_intents": len(pending),
    }


def main():
    try:
        result = run_probe(
            firestore.Client(project=PROJECT, database=DATABASE),
            firestore_admin_v1.FirestoreAdminClient(),
            datetime.now(UTC),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"classification": "captain_persistence_probe_failed", "postable": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
