"""Captain-only NO-POST deployment composition. There is deliberately no X client."""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from flask import jsonify, request

from fpl_bot.api import FplApiClient
from fpl_bot.captain_cloud_tasks import CaptainCloudTasksAdapter, CaptainCloudTasksConfig
from fpl_bot.captain_compute import CaptainComputeAdapter, CaptainComputeReconciler, ComputeTarget
from fpl_bot.captain_controller import CaptainController
from fpl_bot.captain_firestore import FirestoreCaptainRepository, FirestoreCaptainVmOperations
from fpl_bot.captain_http import (
    CaptainAuthConfig,
    GoogleOidcAuthorizer,
    WorkerControllerService,
    create_captain_app,
    default_google_token_verifier,
)
from fpl_bot.captain_no_post_audit import AuditedValidator, FirestoreNoPostAudit
from fpl_bot.captain_serialization import to_document
from fpl_bot.captain_state import GenerationStatus, PostKey, StateConflict, VmPhase
from fpl_bot.captain_validation import CaptainCandidateValidator
from fpl_bot.captain_vm_operations import OperationPhase, VmAction
from fpl_bot.events import parse_events, select_next_event


class UtcClock:
    def now(self):
        return datetime.now(UTC)


@dataclass(frozen=True)
class NoPostConfig:
    project: str
    database: str
    origin: str
    worker_email: str
    tasks_email: str
    planner_email: str
    destination_user_id: str

    def __post_init__(self):
        if self.project != "fpl-frosty-bot-v1" or self.database != "captain-state":
            raise ValueError("Captain rehearsal must use its isolated database")
        if self.origin != "https://captain-controller-524790767721.europe-west2.run.app":
            raise ValueError("Captain-only controller origin required")
        if len({self.worker_email, self.tasks_email, self.planner_email}) != 3:
            raise ValueError("distinct Captain invocation identities required")
        if any(
            not email.startswith("captain-")
            or not email.endswith(f"@{self.project}.iam.gserviceaccount.com")
            for email in (self.worker_email, self.tasks_email, self.planner_email)
        ):
            raise ValueError("Captain-only identities required")
        if self.destination_user_id != "1":
            raise ValueError("NO-POST rehearsal requires diagnostic destination identity")
        if urlsplit(self.origin).path:
            raise ValueError("controller origin must not contain a route")

    @classmethod
    def environment(cls):
        names = (
            "PROJECT",
            "DATABASE",
            "ORIGIN",
            "WORKER_EMAIL",
            "TASKS_EMAIL",
            "PLANNER_EMAIL",
            "DESTINATION_USER_ID",
        )
        return cls(*(os.environ["CAPTAIN_" + name] for name in names))


class NoPostDriver:
    """One bounded reconciliation step per request; never wait through VM boot."""

    def __init__(self, controller, scheduler, compute):
        self.controller, self.scheduler, self.compute = controller, scheduler, compute

    def advance(self):
        c = self.controller
        c.reconcile_vm()
        lease = c.repository.vm_use()
        outcome = "idle"
        if lease is not None:
            start = c.vm_operations.get(lease.lease_id, VmAction.START)
            action = VmAction.STOP if lease.phase == VmPhase.STOPPING else VmAction.START
            if (
                action == VmAction.STOP
                and start
                and start.phase not in {OperationPhase.COMPLETED, OperationPhase.FAILED}
            ):
                # No STOP request until the existing durable START settles.
                outcome = self.compute.reconcile(lease.lease_id, VmAction.START).value
            else:
                outcome = self.compute.submit(lease.lease_id, action).value
                operation = c.vm_operations.get(lease.lease_id, action)
                if operation.phase == OperationPhase.COMPLETED:
                    if action == VmAction.STOP:
                        c.complete_cleanup(lease.lease_id, lease.generation_id)
                    elif (
                        c.repository.generation(lease.generation_id).status
                        == GenerationStatus.WARMING
                    ):
                        c.confirm_runtime_ready(lease.generation_id)
        c.reconcile_tasks(self.scheduler)
        return outcome


def compose(
    config,
    repository,
    operations,
    source,
    clock,
    scheduler,
    provider,
    authorizer,
    *,
    audit_sink=None,
):
    """Injectable composition for offline tests and the isolated real cloud runtime."""
    controller = CaptainController(repository, source, clock, operations)
    service = WorkerControllerService(repository, source, clock, config.destination_user_id)
    compute = CaptainComputeReconciler(repository, operations, provider, clock.now)
    driver = NoPostDriver(controller, scheduler, compute)
    validator = CaptainCandidateValidator(repository, source, clock)
    app = create_captain_app(
        service,
        controller,
        authorizer,
        CaptainAuthConfig(config.origin, config.origin, config.worker_email, config.tasks_email),
        candidate_validator=AuditedValidator(validator, audit_sink) if audit_sink else validator,
    )

    @app.post("/captain/control/tick")
    def tick():
        caller = authorizer.authorize(request.headers.get("Authorization", ""), config.origin)
        if caller.email != config.planner_email:
            raise PermissionError("planner identity denied")
        planned = controller.plan(config.destination_user_id)
        outcome = driver.advance()
        return jsonify(
            {
                "no_post": True,
                "vm": outcome,
                "generations": [str(p.generation.assignment.generation_id) for p in planned],
            }
        )

    @app.post("/captain/control/inspect")
    def inspect():
        caller = authorizer.authorize(request.headers.get("Authorization", ""), config.origin)
        if caller.email != config.planner_email:
            raise PermissionError("planner identity denied")
        event = select_next_event(
            parse_events(source.fetch_bootstrap_static()["events"]), clock.now()
        )
        generation = repository.current(PostKey(config.destination_user_id, event.event_id))
        lease = repository.vm_use()
        return jsonify(
            {
                "no_post": True,
                "database": config.database,
                "generation": to_document(generation),
                "vm_lease": to_document(lease),
                "operations": [
                    to_document(operations.get(lease.lease_id, action)) for action in VmAction
                ]
                if lease
                else [],
            }
        )

    @app.after_request
    def reconcile_after_delivery(response):
        if response.status_code < 300 and (
            request.path.startswith("/captain/tasks/") or request.path == "/captain/worker/handoff"
        ):
            try:
                driver.advance()
            except Exception:
                # Keep durable state/intent for the next tick. Do not expose
                # provider response bodies or turn an acknowledged handoff into
                # an ambiguous transport failure merely because cleanup failed.
                app.logger.error("captain_reconciliation_pending")
        response.headers["X-Captain-No-Post"] = "true"
        return response

    @app.errorhandler(StateConflict)
    def conflict(_):
        return jsonify({"error": "captain_state_conflict", "no_post": True}), 409

    return app


def create_app():
    config = NoPostConfig.environment()
    kwargs = {"project": config.project, "database": config.database}
    repository, operations = (
        FirestoreCaptainRepository(**kwargs),
        FirestoreCaptainVmOperations(**kwargs),
    )
    scheduler = CaptainCloudTasksAdapter(
        CaptainCloudTasksConfig(
            config.project,
            "europe-west2",
            "captain-orchestration",
            config.origin,
            config.tasks_email,
            config.origin,
        )
    )
    provider = CaptainComputeAdapter.from_default_credentials(
        ComputeTarget(config.project, "europe-west2-b", "captain-browser-london-windows-trial")
    )
    return compose(
        config,
        repository,
        operations,
        FplApiClient(),
        UtcClock(),
        scheduler,
        provider,
        GoogleOidcAuthorizer(default_google_token_verifier),
        audit_sink=FirestoreNoPostAudit(repository.client),
    )
