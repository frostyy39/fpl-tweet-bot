"""Authenticated Captain-only controller boundaries; no X write path."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from flask import Flask, g, jsonify, request

from fpl_bot.captain_controller import CaptainController
from fpl_bot.captain_handoff import ProjectionHandoff
from fpl_bot.captain_orchestration_timing import require_utc, utc_text
from fpl_bot.captain_publication_tasks import instruction_for_candidate
from fpl_bot.captain_release_diagnostics import ReleaseRejectionDiagnostic
from fpl_bot.captain_serialization import from_document
from fpl_bot.captain_state import (
    REHEARSAL_DESTINATION_USER_ID,
    GenerationStatus,
    PostKey,
    StateConflict,
    TaskKind,
)
from fpl_bot.captain_task_handlers import decode_task_payload
from fpl_bot.captain_transport import parse_worker_assignment, worker_assignment_payload
from fpl_bot.captain_worker import (
    HandoffReceipt,
    ReceiptStatus,
    ReleaseGrant,
    WorkerAssignment,
    WorkerStatus,
)
from fpl_bot.errors import FplBotError
from fpl_bot.events import parse_events, select_next_event


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    subject: str
    email: str


class RequestAuthorizer(Protocol):
    def authorize(self, authorization: str, audience: str) -> CallerIdentity: ...


@dataclass(frozen=True, slots=True)
class CaptainAuthConfig:
    worker_audience: str
    task_audience: str
    worker_email: str
    task_email: str

    def __post_init__(self):
        if self.worker_email == self.task_email or not all(
            v
            for v in (self.worker_audience, self.task_audience, self.worker_email, self.task_email)
        ):
            raise ValueError("Captain identities must be distinct and explicit")


class GoogleOidcAuthorizer:
    def __init__(self, verifier):
        self.verifier = verifier

    def authorize(self, authorization: str, audience: str) -> CallerIdentity:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise PermissionError("missing bearer identity")
        try:
            claims = self.verifier(authorization[7:], audience)
        except Exception:
            raise PermissionError("invalid workload identity") from None
        if claims.get("aud") != audience or claims.get("email_verified") is not True:
            raise PermissionError("invalid workload identity")
        return CallerIdentity(str(claims.get("sub", "")), str(claims.get("email", "")))


def default_google_token_verifier(token: str, audience: str) -> dict:
    """Verify Google-issued identity tokens without logging or returning the token."""
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)
    except Exception as exc:
        raise PermissionError("invalid workload identity") from exc


class WorkerControllerService:
    def __init__(self, repository, source, clock, destination_user_id: str):
        self.repository, self.source, self.clock = repository, source, clock
        self.destination_user_id = destination_user_id

    def _now(self):
        now = self.clock.now()
        require_utc(now)
        return now

    def _fresh_generation(self, assignment):
        try:
            event = select_next_event(
                parse_events(self.source.fetch_bootstrap_static()["events"]), self._now()
            )
        except (FplBotError, KeyError, TypeError, ValueError, OSError):
            return None
        generation = self.repository.current(PostKey(self.destination_user_id, assignment.event_id))
        if generation is None or generation.assignment != assignment:
            return None
        rehearsal = self.repository.rehearsal(assignment.generation_id)
        expected_deadline = (
            rehearsal.official_deadline_utc
            if rehearsal is not None
            else assignment.timing.deadline_utc
        )
        if event.event_id != assignment.event_id or event.deadline_utc != expected_deadline:
            return None
        return generation

    def assignment(self, run_id: UUID):
        try:
            event = select_next_event(
                parse_events(self.source.fetch_bootstrap_static()["events"]), self._now()
            )
        except (FplBotError, KeyError, TypeError, ValueError, OSError):
            return None
        generation = self.repository.current(PostKey(self.destination_user_id, event.event_id))
        if generation is None or self._fresh_generation(generation.assignment) != generation:
            return None
        if generation.status not in {
            GenerationStatus.WARMING,
            GenerationStatus.READY,
            GenerationStatus.RELEASED,
        }:
            return None
        return WorkerAssignment(1, generation.assignment, run_id)

    def release(self, work: WorkerAssignment, run_id: UUID):
        if work.attempt_id != run_id:
            raise StateConflict("worker run identity mismatch")
        # External authoritative validation is deliberately outside the later
        # atomic claim. The claim rechecks current generation/attempt state.
        generation = self._fresh_generation(work.assignment)
        if generation is None:
            return False
        now = self._now()
        if now < work.assignment.timing.target_utc:
            return None
        if now > work.assignment.timing.expiry_utc:
            return False
        if generation.status == GenerationStatus.RELEASED:
            attempt = self.repository.claim_acquisition(
                work.assignment.generation_id, work.attempt_id, now
            ).record
            return ReleaseGrant(work, run_id, attempt.claimed_at)
        attempt = self.repository.generation_acquisition(work.assignment.generation_id)
        if attempt and attempt.attempt_id == run_id:
            # Replays must also pass the atomic current-generation precondition;
            # a concurrent supersession cannot revive an existing claim.
            attempt = self.repository.claim_acquisition(
                work.assignment.generation_id, work.attempt_id, now
            ).record
            return ReleaseGrant(work, run_id, attempt.claimed_at)
        if generation.status in {GenerationStatus.WARMING, GenerationStatus.READY}:
            # The worker and release task may arrive on opposite sides of the
            # same target-time boundary.  A current, in-window generation that
            # has not yet observed its durable RELEASED transition is pending,
            # not stale.  The atomic claim remains impossible until RELEASED.
            return None
        return False

    def handoff(self, handoff: ProjectionHandoff, digest: str, health=None):
        handoff.verify_digest(digest)
        if self._fresh_generation(handoff.assignment) is None:
            raise StateConflict("handoff no longer matches fresh official FPL state")
        mutation = self.repository.accept(handoff, self._now())
        if health is not None:
            self.repository.record_session_health(handoff.assignment.generation_id, health)
        return HandoffReceipt(
            handoff.assignment.generation_id,
            handoff.attempt_id,
            digest,
            ReceiptStatus.ACCEPTED if mutation.applied else ReceiptStatus.REPLAY,
        )

    def failure(self, work, run_id, status, release_rejection=None):
        if release_rejection is not None and (
            not isinstance(release_rejection, ReleaseRejectionDiagnostic)
            or status not in {WorkerStatus.RELEASE_REJECTED, WorkerStatus.CONTROLLER_UNAVAILABLE}
            or work is None
        ):
            raise StateConflict("invalid worker release diagnostic")
        if work is None:
            return
        if work.attempt_id != run_id:
            raise StateConflict("worker run identity mismatch")
        generation = self.repository.current(
            PostKey(self.destination_user_id, work.assignment.event_id)
        )
        if generation is None or generation.assignment != work.assignment:
            return
        if release_rejection is not None:
            self.repository.record_release_failure(
                work.assignment,
                run_id,
                status.value,
                release_rejection,
                self._now(),
            )
        target = {
            WorkerStatus.AUTHENTICATION_REQUIRED: GenerationStatus.AUTHENTICATION_REQUIRED,
            WorkerStatus.ACQUISITION_FAILED: GenerationStatus.FAILED,
        }.get(status)
        if target and generation.status in {
            GenerationStatus.PLANNED,
            GenerationStatus.WARMING,
            GenerationStatus.READY,
            GenerationStatus.RELEASED,
            GenerationStatus.ACQUIRING,
        }:
            self.repository.transition(
                generation.assignment.generation_id, generation.status, target, self._now()
            )


class DualPurposeWorkerService:
    """Select one trusted production or immutable no-post assignment, never both."""

    def __init__(
        self,
        repository,
        production: WorkerControllerService,
        rehearsal: WorkerControllerService,
    ):
        if (
            production.destination_user_id == REHEARSAL_DESTINATION_USER_ID
            or rehearsal.destination_user_id != REHEARSAL_DESTINATION_USER_ID
        ):
            raise ValueError("invalid dual-purpose worker destinations")
        self.repository = repository
        self.production = production
        self.rehearsal = rehearsal

    def _now(self):
        return self.production._now()

    def _service(self, assignment):
        generation = self.repository.generation(assignment.generation_id)
        if generation.assignment != assignment:
            raise StateConflict("worker assignment is not the immutable generation")
        binding = self.repository.rehearsal(assignment.generation_id)
        if (
            generation.key.destination_user_id == self.production.destination_user_id
            and binding is None
        ):
            return self.production
        if (
            generation.key.destination_user_id == REHEARSAL_DESTINATION_USER_ID
            and binding is not None
            and binding.event_id == assignment.event_id
            and binding.rehearsal_release_utc == assignment.timing.release_utc
        ):
            return self.rehearsal
        raise StateConflict("worker assignment has no permitted purpose")

    def assignment(self, run_id):
        candidates = []
        for service in (self.production, self.rehearsal):
            work = service.assignment(run_id)
            if work is not None:
                try:
                    if self._service(work.assignment) is service:
                        candidates.append(work)
                except StateConflict:
                    return None
        return candidates[0] if len(candidates) == 1 else None

    def release(self, work, run_id):
        return self._service(work.assignment).release(work, run_id)

    def handoff(self, handoff, digest, health=None):
        return self._service(handoff.assignment).handoff(handoff, digest, health)

    def failure(self, work, run_id, status, release_rejection=None):
        if work is not None:
            self._service(work.assignment).failure(work, run_id, status, release_rejection)


def create_captain_app(
    service,
    controller: CaptainController,
    authorizer,
    auth: CaptainAuthConfig,
    *,
    candidate_validator=None,
    publication_router=None,
):
    app = Flask("captain-controller")

    def authorize_worker():
        caller = authorizer.authorize(
            request.headers.get("Authorization", ""), auth.worker_audience
        )
        if caller.email != auth.worker_email:
            raise PermissionError("worker identity denied")

    def authorize_task():
        caller = authorizer.authorize(request.headers.get("Authorization", ""), auth.task_audience)
        if caller.email != auth.task_email:
            raise PermissionError("task identity denied")

    @app.errorhandler(PermissionError)
    def denied(_):
        return jsonify({"error": "unauthorized"}), 403

    @app.errorhandler(ValueError)
    def invalid(_):
        return jsonify({"error": "captain_request_rejected"}), 409

    @app.errorhandler(StateConflict)
    def conflict(_):
        return jsonify({"error": "captain_request_rejected"}), 409

    @app.post("/captain/worker/assignment")
    def assignment():
        authorize_worker()
        data = request.get_json(force=True)
        work = service.assignment(UUID(data["run_id"]))
        return ("", 204) if work is None else (jsonify(worker_assignment_payload(work)), 200)

    @app.post("/captain/worker/release")
    def release():
        authorize_worker()
        data = request.get_json(force=True)
        work, run_id = parse_worker_assignment(data["work"]), UUID(data["run_id"])
        grant = service.release(work, run_id)
        if grant is None:
            return jsonify({"status": "pending"}), 202
        if grant is False:
            return jsonify({"status": "stale"}), 410
        return jsonify(
            {
                "work": worker_assignment_payload(grant.work),
                "run_id": str(grant.run_id),
                "issued_at": utc_text(grant.issued_at),
                "server_sent_at": utc_text(service._now()),
            }
        )

    @app.post("/captain/worker/handoff")
    def handoff():
        authorize_worker()
        data = request.get_json(force=True)
        health = from_document(data["health"]) if data.get("health") else None
        received = ProjectionHandoff.from_payload(data["handoff"])
        receipt = service.handoff(received, data["digest"], health)
        g.captain_destination = controller.repository.generation(
            received.assignment.generation_id
        ).key.destination_user_id
        return jsonify(
            {
                "generation_id": str(receipt.generation_id),
                "attempt_id": str(receipt.attempt_id),
                "payload_digest": receipt.payload_digest,
                "status": receipt.status.value,
            }
        )

    @app.post("/captain/worker/failure")
    def failure():
        authorize_worker()
        data = request.get_json(force=True)
        if type(data) is not dict or set(data) not in (
            {"work", "run_id", "status"},
            {"work", "run_id", "status", "release_rejection"},
        ):
            raise ValueError("invalid worker failure envelope")
        diagnostic = data.get("release_rejection")
        service.failure(
            parse_worker_assignment(data["work"]) if data["work"] else None,
            UUID(data["run_id"]),
            WorkerStatus(data["status"]),
            ReleaseRejectionDiagnostic.from_payload(diagnostic) if diagnostic is not None else None,
        )
        return jsonify({"status": "recorded"})

    @app.post("/captain/tasks/<kind>")
    def task(kind):
        authorize_task()
        data = request.get_json(force=True)
        envelope = decode_task_payload(data)
        if envelope.kind.value != kind:
            raise StateConflict("task envelope conflict")
        result = controller.deliver(envelope)
        g.captain_destination = envelope.destination_user_id
        if (
            envelope.kind == TaskKind.PUBLISH
            and candidate_validator is not None
            and result.status == "publish_eligible_no_write"
        ):
            assignment = envelope.assignment
            generation = controller.repository.generation(assignment.generation_id)
            attempt = controller.repository.generation_acquisition(assignment.generation_id)
            if attempt is None or attempt.handoff is None:
                raise StateConflict("publish lacks accepted handoff")
            candidate = candidate_validator.validate(generation.key, attempt.handoff)
            record = controller.repository.candidate(assignment.generation_id)
            if controller.repository.rehearsal(assignment.generation_id) is not None:
                if record is None:
                    raise StateConflict("validated rehearsal candidate was not persisted")
                return jsonify(
                    {
                        "status": "validated_non_postable_rehearsal",
                        "event_id": candidate.key.event_id,
                        "event_code": candidate.assignment.event_code,
                        "weighted_length": candidate.weighted_length,
                    }
                )
            if publication_router is not None:
                if publication_router.destination_user_id != generation.key.destination_user_id:
                    raise StateConflict("publisher destination does not match generation")
                if record is None:
                    raise StateConflict("validated candidate was not persisted")
                instruction = instruction_for_candidate(record)
                confirmation = publication_router.ensure(instruction, record.validated_at_utc)
                return jsonify(
                    {
                        "status": "publication_enqueued",
                        "event_id": candidate.key.event_id,
                        "event_code": candidate.assignment.event_code,
                        "weighted_length": candidate.weighted_length,
                        "publication_identity": confirmation.identity,
                        "publication_digest": confirmation.digest,
                    }
                )
            return jsonify(
                {
                    "status": "validated_candidate",
                    "event_id": candidate.key.event_id,
                    "event_code": candidate.assignment.event_code,
                    "weighted_length": candidate.weighted_length,
                }
            )
        return jsonify(
            {
                "status": result.status,
                "retry_at": utc_text(result.retry_at) if result.retry_at else None,
            }
        )

    return app
