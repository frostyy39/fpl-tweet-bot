import json
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import AlreadyExists
from test_captain_validation import KEY, build

from fpl_bot.captain_no_post_audit import AuditedValidator, FirestoreNoPostAudit, candidate_audit


def test_audit_preserves_exact_utf8_candidate_evidence_without_posting_authority():
    validator, _, _, _, handoff = build()
    candidate = validator.validate(KEY, handoff)
    audit = candidate_audit(candidate, handoff)
    saved = json.dumps(audit, ensure_ascii=False).encode("utf-8")
    assert json.loads(saved.decode("utf-8"))["tweet"] == candidate.tweet
    assert "João" in saved.decode("utf-8")
    assert audit["postable"] is False
    assert audit["top_three"][0]["source_ordinal"] == 1
    assert audit["top_three"][0]["official_ownership"] == "10.0"
    assert audit["differential"]["official_ownership"] == "9.9"
    assert audit["handoff_digest"] == handoff.payload_digest
    assert not {"cookies", "credentials", "token", "browser_state"} & audit.keys()
    writes = []
    assert AuditedValidator(validator, writes.append).validate(KEY, handoff) == candidate
    assert writes == [audit]


def test_audit_does_not_write_after_validation_rejection():
    writes = []

    def reject(*_):
        raise ValueError("late")

    validator = AuditedValidator(SimpleNamespace(validate=reject), writes.append)
    with pytest.raises(ValueError):
        validator.validate(None, None)
    assert writes == []


def test_firestore_audit_replay_compares_exact_immutable_content():
    documents = {}

    class Document:
        def __init__(self, identity):
            self.identity = identity

        def create(self, payload):
            if self.identity in documents:
                raise AlreadyExists("exists")
            documents[self.identity] = payload

        def get(self):
            return SimpleNamespace(to_dict=lambda: documents[self.identity])

    client = SimpleNamespace(
        _database_string="projects/fpl-frosty-bot-v1/databases/captain-state",
        collection=lambda name: SimpleNamespace(document=Document),
    )
    sink = FirestoreNoPostAudit(client)
    sink({"postable": False, "tweet": "🧢 João"})
    sink({"postable": False, "tweet": "🧢 João"})
    assert len(documents) == 1
    with pytest.raises(ValueError):
        FirestoreNoPostAudit(SimpleNamespace(_database_string="(default)"))
