"""Captain-only Firestore transactions. No resource creation or external task/VM/X calls."""

import hashlib
import json
from collections.abc import MutableMapping
from functools import wraps
from uuid import UUID

from fpl_bot.captain_memory_repository import InMemoryCaptainRepository
from fpl_bot.captain_serialization import InvalidCaptainDocument, from_document, to_document
from fpl_bot.captain_state import (
    AcquisitionAttempt,
    Generation,
    IntentStatus,
    PostingRecord,
    PostKey,
    SessionHealthEvidence,
    TaskIntent,
    TaskKind,
    VmUseLease,
)
from fpl_bot.captain_vm_operations import InMemoryVmOperations, VmAction, VmOperation

PREFIX = "captain_v1_"
SPECS = {
    "generations": (UUID, Generation),
    "current": (PostKey, UUID),
    "attempts": (UUID, AcquisitionAttempt),
    "generation_attempt": (UUID, UUID),
    "posts": (PostKey, PostingRecord),
    "intents": (tuple, TaskIntent),
    "retired_vm": (UUID, VmUseLease),
    "health": (UUID, tuple),
    "assignment_index": (UUID, UUID),
    "claim_index": (UUID, PostKey),
    "vm": (str, (VmUseLease, type(None))),
    "operations": (tuple, VmOperation),
}


def document_id(key):
    return hashlib.sha256(
        json.dumps(to_document(key), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _routing(value):
    if isinstance(value, TaskIntent):
        return {
            "generation": str(value.generation_id),
            "pending": value.status == IntentStatus.PENDING,
        }
    return {}


def _validate_entry(name, key, value):
    key_type, value_type = SPECS[name]
    if type(key) is not key_type or not isinstance(value, value_type):
        raise InvalidCaptainDocument("Captain record type mismatch")
    if name in {"intents", "operations"}:
        enum_type = TaskKind if name == "intents" else VmAction
        if len(key) != 2 or type(key[0]) is not UUID or type(key[1]) is not enum_type:
            raise InvalidCaptainDocument("Captain compound key type mismatch")
    if name == "intents" and key != (value.generation_id, value.kind):
        raise InvalidCaptainDocument("task identity mismatch")
    if name == "operations" and key != (value.lease_id, value.action):
        raise InvalidCaptainDocument("operation identity mismatch")
    bindings = {
        "generations": lambda: value.assignment.generation_id,
        "attempts": lambda: value.attempt_id,
        "posts": lambda: value.key,
        "retired_vm": lambda: value.lease_id,
    }
    if name in bindings and bindings[name]() != key:
        raise InvalidCaptainDocument("record identity mismatch")
    if name == "health" and (
        not all(isinstance(v, SessionHealthEvidence) for v in value)
        or any(b.observed_at <= a.observed_at for a, b in zip(value, value[1:], strict=False))
    ):
        raise InvalidCaptainDocument("session evidence mismatch")
    if name == "vm" and key != "active":
        raise InvalidCaptainDocument("VM singleton mismatch")


class _Map(MutableMapping):
    """Read-through transaction snapshot, with writes buffered until every read finishes."""

    def __init__(self, unit, name):
        self.unit, self.name = unit, name
        self.collection = unit.client.collection(PREFIX + name)
        self.cache, self.dirty = {}, {}
        self.values_loader = None

    def _unpack(self, snapshot, expected_key=None):
        data = snapshot.to_dict()
        if type(data) is not dict or set(data) != {"entry", "routing"}:
            raise InvalidCaptainDocument("invalid Captain entry envelope")
        entry = from_document(data["entry"])
        if type(entry) is not tuple or len(entry) != 2:
            raise InvalidCaptainDocument("invalid Captain entry")
        key, value = entry
        _validate_entry(self.name, key, value)
        if (expected_key is not None and key != expected_key) or snapshot.id != document_id(key):
            raise InvalidCaptainDocument("Captain document identity mismatch")
        if data["routing"] != _routing(value):
            raise InvalidCaptainDocument("Captain routing mismatch")
        self.cache[key] = value
        return value

    def __getitem__(self, key):
        if key not in self.cache:
            snapshot = self.collection.document(document_id(key)).get(transaction=self.unit.tx)
            if not snapshot.exists:
                self.cache[key] = _MISSING
            else:
                self._unpack(snapshot, key)
        value = self.cache[key]
        if value is _MISSING:
            raise KeyError(key)
        try:
            if self.name == "current" and self.unit.maps["generations"][value].key != key:
                raise InvalidCaptainDocument("current generation binding mismatch")
            if (
                self.name == "generation_attempt"
                and self.unit.maps["attempts"][value].generation_id != key
            ):
                raise InvalidCaptainDocument("generation attempt binding mismatch")
        except KeyError:
            raise InvalidCaptainDocument("missing Captain referenced record") from None
        return value

    def __setitem__(self, key, value):
        _validate_entry(self.name, key, value)
        # Also read absent documents: uniqueness conflicts participate in transaction retry.
        old = self.get(key, _MISSING)
        self.cache[key] = value
        if old != value:
            self.dirty[key] = value
        if self.name == "generations":
            self.unit.maps["assignment_index"][value.assignment.assignment_id] = key
        if self.name == "posts":
            for attempt in value.attempts:
                self.unit.maps["claim_index"][attempt.claim_id] = key

    def __delitem__(self, key):
        raise TypeError("Captain audit records are not deleted")

    def values(self):
        if self.values_loader is None:
            raise RuntimeError("unbounded Captain scan prohibited")
        return self.values_loader()

    def __iter__(self):
        self.values()
        return iter(k for k, v in self.cache.items() if v is not _MISSING)

    def __len__(self):
        return len(tuple(iter(self)))

    def query(self, field, value):
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self.collection.where(filter=FieldFilter(field, "==", value))
        for snapshot in query.stream(transaction=self.unit.tx):
            self._unpack(snapshot)
        return tuple(v for v in self.cache.values() if v is not _MISSING)


_MISSING = object()


class _Unit:
    def __init__(self, client, transaction):
        self.client, self.tx = client, transaction
        self.maps = {name: _Map(self, name) for name in SPECS}

    def flush(self):
        # Serialize/validate ALL data before buffering any SDK writes. No more reads.
        writes = [
            (
                mapping.collection.document(document_id(key)),
                {"entry": to_document((key, value)), "routing": _routing(value)},
            )
            for mapping in self.maps.values()
            for key, value in mapping.dirty.items()
        ]
        for ref, data in writes:
            self.tx.set(ref, data)

    def reference(self, method, args):
        repo = InMemoryCaptainRepository()
        for name in (
            "generations",
            "current",
            "attempts",
            "generation_attempt",
            "posts",
            "intents",
            "retired_vm",
            "health",
        ):
            setattr(repo, "_" + name, self.maps[name])
        if method == "plan":
            assignment, previous = args[1], args[2]

            def existing_assignment():
                gid = self.maps["assignment_index"].get(assignment.assignment_id)
                if gid is None:
                    return ()
                record = repo._generations[gid]
                if record.assignment.assignment_id != assignment.assignment_id:
                    raise InvalidCaptainDocument("assignment index binding mismatch")
                return (record,)

            repo._generations.values_loader = existing_assignment
            repo._intents.values_loader = lambda: repo._intents.query(
                "routing.generation", str(previous)
            )
        if method == "claim_post":

            def existing_claim():
                key = self.maps["claim_index"].get(args[1])
                if key is None:
                    return ()
                record = repo._posts[key]
                if not any(a.claim_id == args[1] for a in record.attempts):
                    raise InvalidCaptainDocument("claim index binding mismatch")
                return (record,)

            repo._posts.values_loader = existing_claim
        if method == "pending_intents":
            repo._intents.values_loader = lambda: repo._intents.query("routing.pending", True)
        if method in {"vm_use", "acquire_vm", "begin_cleanup", "complete_cleanup"}:
            repo._vm = self.maps["vm"].get("active")
        return repo


class _FirestoreBase:
    def __init__(self, *, project: str, database: str, client=None, transactional_wrapper=None):
        if (
            not isinstance(project, str)
            or not project
            or "/" in project
            or not isinstance(database, str)
            or not database
            or "/" in database
        ):
            raise ValueError("explicit Firestore project and database required")
        from google.cloud import firestore

        self.client = (
            client if client is not None else firestore.Client(project=project, database=database)
        )
        # A caller cannot accidentally supply a client bound to another database.
        expected = f"projects/{project}/databases/{database}"
        if self.client.project != project or self.client._database_string != expected:
            raise ValueError("Firestore client database identity mismatch")
        self.transactional = transactional_wrapper or firestore.transactional


class FirestoreCaptainRepository(_FirestoreBase):
    """Same transitions as the reference model, executed over transactional point reads."""

    def _invoke(self, method, *args, **kwargs):
        # Normalize keyword calls against the reference signature before retryable work.
        import inspect

        bound = inspect.signature(getattr(InMemoryCaptainRepository, method)).bind(
            None, *args, **kwargs
        )
        bound.apply_defaults()
        args = tuple(v for k, v in bound.arguments.items() if k != "self")

        def operation(tx):
            unit = _Unit(self.client, tx)
            repo = unit.reference(method, args)
            result = getattr(repo, method)(*args)
            if method in {"acquire_vm", "begin_cleanup", "complete_cleanup"}:
                unit.maps["vm"]["active"] = repo._vm
            unit.flush()
            return result

        return self.transactional(operation)(self.client.transaction())


class FirestoreCaptainVmOperations(_FirestoreBase):
    def _invoke(self, method, *args, **kwargs):
        def operation(tx):
            unit = _Unit(self.client, tx)
            # Dispatch reservation must read the singleton VM owner, current
            # generation and operation records in this same retryable transaction.
            repository = unit.reference("vm_use", ())
            ledger = InMemoryVmOperations(repository)
            ledger._operations = unit.maps["operations"]
            result = getattr(ledger, method)(*args, **kwargs)
            unit.flush()
            return result

        return self.transactional(operation)(self.client.transaction())


def _forward(reference, name):
    @wraps(getattr(reference, name))
    def method(self, *args, **kwargs):
        return self._invoke(name, *args, **kwargs)

    return method


# Explicit public API; no arbitrary operation names from persisted input or a transport.
for _name in (
    "plan",
    "generation",
    "current",
    "transition",
    "claim_acquisition",
    "acquisition",
    "generation_acquisition",
    "accept",
    "posting",
    "claim_post",
    "start_write",
    "finish_post",
    "pending_intents",
    "task_intent",
    "acknowledge_intent",
    "vm_use",
    "acquire_vm",
    "begin_cleanup",
    "complete_cleanup",
    "record_session_health",
    "session_health",
):
    setattr(FirestoreCaptainRepository, _name, _forward(InMemoryCaptainRepository, _name))
for _name in (
    "request",
    "get",
    "acknowledge",
    "finish",
    "stop_is_settled",
    "reserve_dispatch",
    "record_dispatch",
):
    setattr(FirestoreCaptainVmOperations, _name, _forward(InMemoryVmOperations, _name))
