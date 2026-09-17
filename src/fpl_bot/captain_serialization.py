"""Versioned allowlist codec for Captain persistence, never arbitrary object loading."""

import json
import re
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import get_args, get_origin, get_type_hints
from uuid import UUID

from fpl_bot.captain_handoff import (
    AuthenticationStatus,
    CaptainAssignment,
    CleanupStatus,
    CompletenessStatus,
    ProjectionHandoff,
    ProjectionRecord,
    RowCounts,
)
from fpl_bot.captain_orchestration_timing import CaptainTiming, utc_text
from fpl_bot.captain_state import (
    AcquisitionAttempt,
    AttemptStatus,
    Generation,
    GenerationStatus,
    IntentStatus,
    PostingAttempt,
    PostingRecord,
    PostingStatus,
    PostKey,
    RefreshObservation,
    SessionExpiryKind,
    SessionHealthEvidence,
    StateConflict,
    TaskIntent,
    TaskKind,
    VmPhase,
    VmUseLease,
)
from fpl_bot.captain_vm_operations import (
    DispatchStatus,
    OperationPhase,
    VmAction,
    VmDispatchReservation,
    VmOperation,
)

RECORDS = {
    t.__name__: t
    for t in (
        CaptainAssignment,
        CaptainTiming,
        ProjectionHandoff,
        ProjectionRecord,
        RowCounts,
        PostKey,
        Generation,
        AcquisitionAttempt,
        PostingAttempt,
        PostingRecord,
        TaskIntent,
        VmUseLease,
        SessionHealthEvidence,
        VmDispatchReservation,
        VmOperation,
    )
}
ENUMS = {
    t.__name__: t
    for t in (
        AuthenticationStatus,
        CleanupStatus,
        CompletenessStatus,
        AttemptStatus,
        GenerationStatus,
        IntentStatus,
        PostingStatus,
        RefreshObservation,
        SessionExpiryKind,
        TaskKind,
        VmPhase,
        OperationPhase,
        DispatchStatus,
        VmAction,
    )
}


class InvalidCaptainDocument(StateConflict):
    """Malformed/incompatible durable state; never include raw persisted data in errors."""


def _typed(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        return any(_typed(value, a) for a in args)
    if origin is tuple:
        return type(value) is tuple and (
            all(_typed(v, args[0]) for v in value)
            if args[-1:] == (Ellipsis,)
            else len(value) == len(args)
            and all(_typed(v, a) for v, a in zip(value, args, strict=True))
        )
    return type(value) is annotation


def _history(history, first, edges):
    if not history or history[0][0] != first:
        raise ValueError("history origin")
    for (old, before), (new, after) in zip(history, history[1:], strict=False):
        if after < before or new not in edges.get(old, set()):
            raise ValueError("history transition")


def _validate(value):
    if is_dataclass(value):
        hints = get_type_hints(type(value))
        if any(not _typed(getattr(value, f.name), hints[f.name]) for f in fields(value)):
            raise ValueError("record field type")
    if isinstance(value, Generation):
        g = GenerationStatus
        edges = {
            g.PLANNED: {g.WARMING},
            g.WARMING: {g.READY},
            g.READY: {g.RELEASED},
            g.RELEASED: {g.ACQUIRING},
            g.ACQUIRING: {g.ACCEPTED},
        }
        for state in list(edges):
            edges[state] |= {g.FAILED, g.AUTHENTICATION_REQUIRED, g.MISSED}
        for state in g:
            if state != g.CANCELLED_STALE:
                edges.setdefault(state, set()).add(g.CANCELLED_STALE)
        _history(value.history, g.PLANNED, edges)
        if value.version < 1 or value.key.event_id != value.assignment.event_id:
            raise ValueError("generation binding")
        for state, at in value.history:
            timing = value.assignment.timing
            if state == g.MISSED and at <= timing.expiry_utc:
                raise ValueError("missed history")
            if state in {g.WARMING, g.READY} and not timing.warmup_utc <= at <= timing.expiry_utc:
                raise ValueError("warmup history")
            if state in {g.RELEASED, g.ACQUIRING, g.ACCEPTED} and not timing.permits_new_attempt(
                at
            ):
                raise ValueError("release history")
    if isinstance(value, AcquisitionAttempt):
        if value.updated_at < value.claimed_at:
            raise ValueError("attempt chronology")
        if (value.status == AttemptStatus.ACCEPTED) != (value.handoff is not None):
            raise ValueError("acceptance evidence")
        if value.handoff:
            if (
                value.attempt_id != value.handoff.attempt_id
                or value.generation_id != value.handoff.assignment.generation_id
                or value.claimed_at > value.handoff.acquisition_started_utc
            ):
                raise ValueError("attempt binding")
            value.handoff.require_eligible(value.handoff.assignment, value.updated_at)
    if isinstance(value, PostingAttempt):
        p = PostingStatus
        _history(
            value.history,
            p.CLAIMED,
            {
                p.CLAIMED: {p.WRITE_STARTED, p.FAILED_BEFORE_WRITE},
                p.WRITE_STARTED: {p.UNCERTAIN, p.SUCCEEDED},
                p.UNCERTAIN: {p.SUCCEEDED},
            },
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value.payload_digest):
            raise ValueError("digest")
        if (value.status == p.SUCCEEDED) != (value.post_id is not None):
            raise ValueError("post evidence")
        if value.post_id is not None and not re.fullmatch(r"[1-9][0-9]{0,31}", value.post_id):
            raise ValueError("post identity")
    if isinstance(value, PostingRecord):
        if len({a.claim_id for a in value.attempts}) != len(value.attempts):
            raise ValueError("duplicate claim")
        if any(a.status != PostingStatus.FAILED_BEFORE_WRITE for a in value.attempts[:-1]):
            raise ValueError("posting barrier bypass")
        if any(
            b.history[0][1] < a.history[-1][1]
            for a, b in zip(value.attempts, value.attempts[1:], strict=False)
        ):
            raise ValueError("posting chronology")


def _encode(value):
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is UUID:
        if value.int == 0:
            raise ValueError("zero identity")
        return {"uuid": str(value)}
    if type(value) is datetime:
        return {"utc": utc_text(value)}
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("nonfinite decimal")
        return {"decimal": str(value)}
    if isinstance(value, Enum) and type(value).__name__ in ENUMS:
        return {"enum": type(value).__name__, "value": value.value}
    if type(value) is tuple:
        # Maps between arrays avoid Firestore's nested-array prohibition.
        return {"tuple": [_encode(v) for v in value]}
    if type(value).__name__ in RECORDS and type(value) is RECORDS[type(value).__name__]:
        _validate(value)
        result = {
            "record": type(value).__name__,
            "fields": {f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        }
        if isinstance(value, ProjectionHandoff):
            result["digest"] = value.payload_digest
        return result
    raise ValueError("unsupported record")


def _decode(data):
    if data is None or type(data) in (str, int, bool):
        return data
    if type(data) is not dict:
        raise ValueError("invalid durable value")
    if set(data) == {"uuid"}:
        return UUID(data["uuid"])
    if set(data) == {"utc"}:
        return datetime.fromisoformat(data["utc"].replace("Z", "+00:00"))
    if set(data) == {"decimal"}:
        return Decimal(data["decimal"])
    if set(data) == {"enum", "value"}:
        return ENUMS[data["enum"]](data["value"])
    if set(data) == {"tuple"} and type(data["tuple"]) is list:
        return tuple(_decode(v) for v in data["tuple"])
    expected = (
        {"record", "fields", "digest"}
        if data.get("record") == "ProjectionHandoff"
        else {"record", "fields"}
    )
    if set(data) != expected:
        raise ValueError("unknown fields")
    cls = RECORDS[data["record"]]
    if type(data["fields"]) is not dict:
        raise ValueError("record fields")
    expected = {f.name for f in fields(cls)}
    field_data = dict(data["fields"])
    # Legacy VM-operation records remain readable for audit only. Protocol zero
    # deliberately cannot be dispatched without operator reconciliation.
    if cls is VmOperation:
        missing = expected - set(field_data)
        if missing in (
            {"dispatch_protocol", "dispatch"},
            {"provider_operation_id", "dispatch_protocol", "dispatch"},
        ):
            field_data.setdefault("provider_operation_id", None)
            field_data["dispatch_protocol"] = 0
            field_data["dispatch"] = None
    if set(field_data) != expected:
        raise ValueError("record fields")
    value = cls(**{k: _decode(v) for k, v in field_data.items()})
    _validate(value)
    return value


def to_document(value) -> dict:
    try:
        result = {"schema_version": 1, "value": _encode(value)}
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 750_000:
            raise ValueError("record exceeds safe size")
        return result
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
        raise InvalidCaptainDocument("invalid Captain durable record") from None


def _canonical_for_input(original, canonical):
    """Remove only explicitly supported legacy fields, including nested records."""
    if type(original) is not dict or type(canonical) is not dict:
        return canonical
    if set(original) == {"tuple"} and set(canonical) == {"tuple"}:
        canonical["tuple"] = [
            _canonical_for_input(old, new)
            for old, new in zip(original["tuple"], canonical["tuple"], strict=True)
        ]
    if original.get("record") == "VmOperation" and canonical.get("record") == "VmOperation":
        original_fields = original.get("fields", {})
        for name in ("provider_operation_id", "dispatch_protocol", "dispatch"):
            if name not in original_fields:
                canonical["fields"].pop(name, None)
    return canonical


def from_document(data):
    try:
        if type(data) is not dict or set(data) != {"schema_version", "value"}:
            raise ValueError("document fields")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("schema version")
        value = _decode(data["value"])
        canonical = to_document(value)
        canonical["value"] = _canonical_for_input(data["value"], canonical["value"])
        if canonical != data:
            raise ValueError("noncanonical or inconsistent document")
        return value
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
        raise InvalidCaptainDocument("invalid Captain durable document") from None
