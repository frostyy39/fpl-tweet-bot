"""Diagnostic candidate evidence only: never a publisher input or permission."""

import hashlib
import json

from google.api_core.exceptions import AlreadyExists

from fpl_bot.captain_tweet import format_projection, render_fixture


def candidate_audit(candidate, handoff):
    def selection(selected):
        official = selected.official
        return {
            "source_ordinal": selected.source_ordinal,
            "projection_rank": selected.projection_rank,
            "official_id": official.player.element_id,
            "web_name": official.player.web_name,
            "projection": format_projection(official.projected_points),
            "official_ownership": str(official.player.selected_by_percent),
            "fixtures": " & ".join(render_fixture(f) for f in official.fixtures),
        }

    return {
        "schema_version": 2,
        "classification": "captain_no_post_diagnostic",
        "purpose": "non_postable_rehearsal",
        "postable": False,
        "destination_user_id": candidate.key.destination_user_id,
        "event_id": candidate.key.event_id,
        "event_code": candidate.assignment.event_code,
        "generation_id": str(candidate.assignment.generation_id),
        "attempt_id": str(candidate.attempt_id),
        "handoff_digest": candidate.accepted_handoff_digest,
        "official_deadline_utc": candidate.official_deadline_utc.isoformat(),
        "rehearsal_release_utc": candidate.assignment.timing.release_utc.isoformat(),
        "target_utc": candidate.assignment.timing.target_utc.isoformat(),
        "validated_at_utc": candidate.validated_at_utc.isoformat(),
        "acquisition_start_utc": handoff.acquisition_started_utc.isoformat(),
        "acquisition_end_utc": handoff.acquisition_ended_utc.isoformat(),
        "raw_rows": handoff.counts.raw,
        "genuine_rows": handoff.counts.genuine,
        "extracted_rows": handoff.counts.extracted,
        "authentication": handoff.authentication.value,
        "cleanup": handoff.cleanup.value,
        "top_three": [selection(s) for s in candidate.top_three],
        "differential": selection(candidate.differential),
        "tweet": candidate.tweet,
        "weighted_length": candidate.weighted_length,
    }


class AuditedValidator:
    def __init__(self, validator, sink):
        self.validator, self.sink = validator, sink

    def validate(self, key, handoff):
        candidate = self.validator.validate(key, handoff)
        self.sink(candidate_audit(candidate, handoff))
        return candidate


class FirestoreNoPostAudit:
    def __init__(self, client):
        if client._database_string != "projects/fpl-frosty-bot-v1/databases/captain-state":
            raise ValueError("isolated Captain audit database required")
        self.client = client

    def __call__(self, audit):
        canonical = json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        ref = self.client.collection("captain_no_post_audits").document(digest)
        try:
            ref.create(audit)
        except AlreadyExists:
            if ref.get().to_dict() != audit:
                raise ValueError("immutable no-post audit conflict") from None
