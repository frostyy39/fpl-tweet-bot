from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded, NotFound

from fpl_bot.captain_cloud_tasks import CaptainTaskProviderError
from fpl_bot.captain_production_controller_runtime import (
    ProductionCaptainControllerConfig,
)
from fpl_bot.captain_publication_tasks import (
    CaptainPublicationTasksAdapter,
    CaptainPublicationTasksConfig,
    instruction_for_candidate,
    publication_task_digest,
    publication_task_identity,
)
from fpl_bot.captain_state import (
    CandidateSelectionEvidence,
    PostKey,
    StateConflict,
    ValidatedCandidateRecord,
    candidate_content_digest,
)

PRODUCTION_ID = "1249335464571650048"
VALIDATED_AT = datetime(2026, 10, 10, 8, 1, tzinfo=UTC)


def candidate(destination=PRODUCTION_ID):
    key = PostKey(destination, 6)
    selections = tuple(
        CandidateSelectionEvidence(
            value,
            value,
            100 + value,
            f"Player {value}",
            Decimal(f"{10 - value}.25"),
            Decimal("5.0"),
            ("ARS (H)",),
        )
        for value in range(1, 5)
    )
    args = (
        key,
        UUID(int=10),
        UUID(int=11),
        "a" * 64,
        "GW6",
        datetime(2026, 10, 10, 10, tzinfo=UTC),
        selections[:3],
        selections[3],
        "canonical Captain tweet",
        200,
    )
    return ValidatedCandidateRecord(
        *args,
        VALIDATED_AT,
        candidate_content_digest(*args),
    )


def config():
    return CaptainPublicationTasksConfig(
        "fpl-frosty-bot-v1",
        "europe-west2",
        "captain-orchestration",
        "https://captain-production-publisher-524790767721.europe-west1.run.app",
        "captain-prod-pub-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com",
        "https://captain-production-publisher-524790767721.europe-west1.run.app",
        PRODUCTION_ID,
    )


class Tasks:
    def __init__(self):
        self.created = None
        self.existing = None
        self.error = None

    def create_task(self, request, *, retry):
        assert retry is None
        self.created = request["task"]
        if self.error:
            raise self.error
        return SimpleNamespace(name=self.created.name)

    def get_task(self, request, *, retry):
        assert retry is None
        if self.existing is None:
            raise NotFound("missing")
        return self.existing


def test_candidate_derives_one_stable_non_text_publication_instruction():
    record = candidate()
    first = instruction_for_candidate(record)
    second = instruction_for_candidate(record)
    assert first == second
    assert first.candidate_digest == record.candidate_digest
    assert first.handoff_digest == record.handoff_digest
    assert "tweet" not in first.to_payload()
    assert publication_task_identity(first) == f"captain-x-{record.generation_id}"
    assert publication_task_digest(first) == publication_task_digest(second)


def test_rehearsal_destination_can_never_route_to_production_publisher():
    with pytest.raises(StateConflict, match="rehearsal"):
        instruction_for_candidate(candidate("1"))


def test_publication_task_is_exact_oidc_authenticated_and_immediate_from_persisted_time():
    client = Tasks()
    adapter = CaptainPublicationTasksAdapter(config(), client)
    instruction = instruction_for_candidate(candidate())
    receipt = adapter.ensure(instruction, VALIDATED_AT)
    assert receipt.identity == publication_task_identity(instruction)
    assert receipt.digest == publication_task_digest(instruction)
    task = client.created
    assert task.schedule_time == VALIDATED_AT
    assert task.http_request.url.endswith("/captain/publisher/execute")
    assert task.http_request.oidc_token.service_account_email.startswith(
        "captain-prod-pub-invoker@"
    )
    assert b"canonical Captain tweet" not in task.http_request.body
    assert b"destination" not in task.http_request.body


def test_duplicate_and_ambiguous_publication_delivery_reconcile_without_new_identity():
    client = Tasks()
    adapter = CaptainPublicationTasksAdapter(config(), client)
    instruction = instruction_for_candidate(candidate())
    intended = adapter._api_task(instruction, VALIDATED_AT)
    client.error, client.existing = AlreadyExists("exists"), intended
    assert adapter.ensure(instruction, VALIDATED_AT).identity == publication_task_identity(
        instruction
    )
    client.error = DeadlineExceeded("unknown")
    assert adapter.ensure(instruction, VALIDATED_AT).identity == publication_task_identity(
        instruction
    )
    client.existing = None
    with pytest.raises(CaptainTaskProviderError) as error:
        adapter.ensure(instruction, VALIDATED_AT)
    assert error.value.category == "publisher_task_create_ambiguous"


@pytest.mark.parametrize(
    "field,value",
    [
        ("destination_user_id", "1732468005336907776"),
        ("publisher_origin", "https://captain-publisher.example.run.app"),
        ("oidc_service_account", "captain-worker@example.iam.gserviceaccount.com"),
    ],
)
def test_production_task_config_rejects_test_arbitrary_or_worker_boundaries(field, value):
    values = (
        config().__dict__
        if hasattr(config(), "__dict__")
        else {name: getattr(config(), name) for name in config().__dataclass_fields__}
    )
    values[field] = value
    with pytest.raises(ValueError):
        CaptainPublicationTasksConfig(**values)


def controller_environment():
    return {
        "CAPTAIN_PROJECT": "fpl-frosty-bot-v1",
        "CAPTAIN_DATABASE": "captain-state",
        "CAPTAIN_ORIGIN": "https://captain-controller-524790767721.europe-west1.run.app",
        "CAPTAIN_WORKER_EMAIL": "captain-worker@fpl-frosty-bot-v1.iam.gserviceaccount.com",
        "CAPTAIN_TASKS_EMAIL": "captain-tasks@fpl-frosty-bot-v1.iam.gserviceaccount.com",
        "CAPTAIN_PLANNER_EMAIL": "captain-planner@fpl-frosty-bot-v1.iam.gserviceaccount.com",
        "CAPTAIN_DESTINATION_USER_ID": PRODUCTION_ID,
        "CAPTAIN_PUBLISHER_ORIGIN": "https://captain-production-publisher-524790767721.europe-west1.run.app",
        "CAPTAIN_PUBLISHER_INVOKER_EMAIL": (
            "captain-prod-pub-invoker@fpl-frosty-bot-v1.iam.gserviceaccount.com"
        ),
    }


def test_production_controller_configuration_is_fixed_and_has_no_test_mode():
    assert (
        ProductionCaptainControllerConfig.environment(controller_environment()).destination_user_id
        == PRODUCTION_ID
    )
    values = controller_environment()
    values["CAPTAIN_DESTINATION_USER_ID"] = "1"
    with pytest.raises(ValueError):
        ProductionCaptainControllerConfig.environment(values)


def test_production_routing_and_arming_files_remain_fail_closed_by_default():
    root = Path(__file__).parents[1]
    controller = (root / "deploy/deploy-captain-production-controller.ps1").read_text()
    publisher = (root / "deploy/deploy-captain-production-publisher.ps1").read_text()
    good_luck = (root / "deploy/deploy-production-good-luck.ps1").read_text()
    emergency = (root / "deploy/emergency-disable-production.ps1").read_text()
    assert "captain-production-publisher" in controller
    assert "CAPTAIN_DESTINATION_USER_ID=$productionUserId" in controller
    assert "PAUSED" in controller and "TERMINATED" in controller
    for script in (publisher, good_luck):
        assert "[switch]$EnablePosting" in script
        assert "production_readiness" in script
        assert "if ($EnablePosting) { 'true' } else { 'false' }" in script
    assert "X_POSTING_ENABLED=false" in emergency
    assert "git reset" not in emergency
    assert "raw stop" in emergency
