import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import AlreadyExists, NotFound, PermissionDenied
from google.cloud import tasks_v2

import fpl_bot.cloud_tasks as cloud_tasks_module
from fpl_bot.cloud_tasks import (
    PAYLOAD_VERSION,
    CloudTaskCreateAmbiguousError,
    CloudTaskCreateDisposition,
    CloudTaskCreateRejectedError,
    CloudTaskDefinitionConflictError,
    CloudTaskNameReservedError,
    CloudTasksConfig,
    CloudTaskValidationError,
    GoogleCloudTasksAdapter,
    GooglePreflightCloudTasksAdapter,
    deterministic_preflight_task_id,
    deterministic_task_id,
    serialize_instruction,
)
from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction

DEADLINE_UTC = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)


class FakeCloudTasksClient:
    def __init__(
        self,
        create_effect: Exception | None = None,
        get_effect: tasks_v2.Task | Exception | None = None,
    ) -> None:
        self.create_effect = create_effect
        self.get_effect = get_effect
        self.calls: list[tuple[Any, Any]] = []
        self.get_calls: list[tuple[Any, Any]] = []

    def create_task(self, request: Any, *, retry: Any) -> Any:
        self.calls.append((request, retry))
        if self.create_effect is not None:
            raise self.create_effect
        return SimpleNamespace(name=request["task"].name)

    def get_task(self, request: Any, *, retry: Any) -> tasks_v2.Task:
        self.get_calls.append((request, retry))
        if isinstance(self.get_effect, Exception):
            raise self.get_effect
        if self.get_effect is None:
            raise AssertionError("Unexpected GetTask call")
        return self.get_effect


def instruction(
    *,
    event_id: int = 3,
    deadline: datetime = DEADLINE_UTC,
) -> ScheduledDeadlineInstruction:
    return ScheduledDeadlineInstruction(event_id, deadline)


def config() -> CloudTasksConfig:
    return CloudTasksConfig(
        project_id="fpl-bot-test",
        location_id="europe-west2",
        queue_id="deadline-posts",
        execution_url="https://fpl-bot-test-abc.europe-west2.run.app/tasks/deadline",
        service_account_email="task-caller@fpl-bot-test.iam.gserviceaccount.com",
    )


def preflight_config() -> CloudTasksConfig:
    return CloudTasksConfig(
        project_id="fpl-bot-test",
        location_id="europe-west2",
        queue_id="deadline-posts",
        execution_url="https://fpl-bot-test-abc.europe-west2.run.app/tasks/preflight",
        service_account_email="task-caller@fpl-bot-test.iam.gserviceaccount.com",
    )


def test_deterministic_task_id_is_stable_and_cloud_tasks_safe() -> None:
    first = deterministic_task_id(instruction())
    second = deterministic_task_id(instruction())

    assert first == second
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,500}", first)
    assert len(first) == 44


def test_changed_deadline_changes_task_id() -> None:
    assert deterministic_task_id(instruction()) != deterministic_task_id(
        instruction(deadline=DEADLINE_UTC + timedelta(minutes=30))
    )


def test_changed_event_id_changes_task_id() -> None:
    assert deterministic_task_id(instruction()) != deterministic_task_id(instruction(event_id=4))


def test_preflight_task_identity_is_stable_distinct_and_changes_with_instruction() -> None:
    task_id = deterministic_preflight_task_id(instruction())

    assert task_id == deterministic_preflight_task_id(instruction())
    assert task_id != deterministic_task_id(instruction())
    assert task_id.startswith("fpl-preflight-")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,500}", task_id)
    assert task_id != deterministic_preflight_task_id(instruction(event_id=4))
    assert task_id != deterministic_preflight_task_id(
        instruction(deadline=DEADLINE_UTC + timedelta(minutes=30))
    )


def test_preflight_adapter_schedules_exactly_five_minutes_early_with_same_payload() -> None:
    client = FakeCloudTasksClient()
    adapter = GooglePreflightCloudTasksAdapter(preflight_config(), client)
    definition = adapter.build_task(instruction())

    result = adapter.create_task(definition)

    assert result is CloudTaskCreateDisposition.CREATED
    assert definition.schedule_time_utc == DEADLINE_UTC - timedelta(minutes=5)
    assert definition.payload == serialize_instruction(instruction())
    task = client.calls[0][0]["task"]
    assert task.name.endswith(f"/tasks/{deterministic_preflight_task_id(instruction())}")
    assert task.schedule_time == DEADLINE_UTC - timedelta(minutes=5)
    assert task.http_request.url == preflight_config().execution_url


def test_duplicate_preflight_task_uses_existing_get_task_verification() -> None:
    client = FakeCloudTasksClient(AlreadyExists("duplicate"))
    adapter = GooglePreflightCloudTasksAdapter(preflight_config(), client)
    definition = adapter.build_task(instruction())
    client.get_effect = adapter._api_task(definition)

    result = adapter.create_task(definition)

    assert result is CloudTaskCreateDisposition.ALREADY_EXISTS
    assert len(client.calls) == 1
    assert len(client.get_calls) == 1
    assert client.calls[0][0]["task"].name == definition.task_name


def test_task_identity_is_independent_of_payload_schema_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = deterministic_task_id(instruction())

    monkeypatch.setattr(cloud_tasks_module, "PAYLOAD_VERSION", 2)

    assert deterministic_task_id(instruction()) == original
    assert json.loads(serialize_instruction(instruction()))["version"] == 2


def test_payload_contains_only_version_and_immutable_instruction() -> None:
    payload = serialize_instruction(instruction())
    decoded = json.loads(payload)

    assert decoded == {
        "expected_deadline_utc": "2026-08-29T10:30:00.000000Z",
        "expected_event_id": 3,
        "version": PAYLOAD_VERSION,
    }
    assert set(decoded) == {"expected_deadline_utc", "expected_event_id", "version"}
    assert b"event_code" not in payload
    assert b"tweet" not in payload
    assert b"token" not in payload
    assert b"secret" not in payload


def test_adapter_submits_exact_schedule_http_target_oidc_and_payload() -> None:
    client = FakeCloudTasksClient()
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())

    result = adapter.create_task(definition)

    assert result is CloudTaskCreateDisposition.CREATED
    assert len(client.calls) == 1
    request, retry = client.calls[0]
    task = request["task"]
    assert request["parent"] == config().queue_name
    assert task.name == definition.task_name
    assert task.schedule_time == DEADLINE_UTC
    assert task.http_request.url == config().execution_url
    assert task.http_request.body == serialize_instruction(instruction())
    assert task.http_request.headers == {"Content-Type": "application/json"}
    assert task.http_request.oidc_token.service_account_email == config().service_account_email
    assert task.http_request.oidc_token.audience == config().execution_url
    assert retry is None


def test_duplicate_name_is_idempotent_and_never_renamed() -> None:
    client = FakeCloudTasksClient(AlreadyExists("duplicate"))
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())
    client.get_effect = adapter._api_task(definition)

    result = adapter.create_task(definition)

    assert result is CloudTaskCreateDisposition.ALREADY_EXISTS
    assert len(client.calls) == 1
    assert client.calls[0][0]["task"].name == definition.task_name
    assert len(client.get_calls) == 1
    get_request, get_retry = client.get_calls[0]
    assert get_request.name == definition.task_name
    assert get_request.response_view is tasks_v2.Task.View.FULL
    assert get_retry is None


def test_duplicate_retained_name_without_current_task_is_not_already_armed() -> None:
    client = FakeCloudTasksClient(
        AlreadyExists("duplicate"),
        NotFound("retained task name"),
    )
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())

    with pytest.raises(CloudTaskNameReservedError) as captured:
        adapter.create_task(definition)

    assert captured.value.task_name == definition.task_name
    assert len(client.calls) == 1
    assert len(client.get_calls) == 1


@pytest.mark.parametrize(
    "mismatch",
    [
        "schedule_time",
        "http_method",
        "payload",
        "execution_url",
        "oidc_service_account",
        "oidc_audience",
    ],
)
def test_duplicate_existing_mismatched_task_fails_closed(mismatch: str) -> None:
    client = FakeCloudTasksClient(AlreadyExists("duplicate"))
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())
    existing = tasks_v2.Task(adapter._api_task(definition))
    if mismatch == "schedule_time":
        existing.schedule_time = DEADLINE_UTC + timedelta(seconds=1)
    elif mismatch == "http_method":
        existing.http_request.http_method = tasks_v2.HttpMethod.GET
    elif mismatch == "payload":
        existing.http_request.body = b'{"different":true}'
    elif mismatch == "execution_url":
        existing.http_request.url = "https://different.example.test/tasks"
    elif mismatch == "oidc_service_account":
        existing.http_request.oidc_token.service_account_email = (
            "different@fpl-bot-test.iam.gserviceaccount.com"
        )
    else:
        existing.http_request.oidc_token.audience = "https://different.example.test"
    client.get_effect = existing

    with pytest.raises(CloudTaskDefinitionConflictError) as captured:
        adapter.create_task(definition)

    assert mismatch in captured.value.mismatched_fields
    assert len(client.calls) == 1
    assert len(client.get_calls) == 1
    assert client.calls[0][0]["task"].name == definition.task_name


def test_ambiguous_create_with_matching_task_is_reconciled() -> None:
    client = FakeCloudTasksClient(TimeoutError("connection ended"))
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())
    client.get_effect = adapter._api_task(definition)

    result = adapter.create_task(definition)

    assert result is CloudTaskCreateDisposition.RECONCILED
    assert len(client.calls) == 1
    assert len(client.get_calls) == 1


def test_ambiguous_create_without_current_task_remains_ambiguous() -> None:
    client = FakeCloudTasksClient(
        TimeoutError("connection ended"),
        NotFound("not found"),
    )
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())

    with pytest.raises(CloudTaskCreateAmbiguousError) as captured:
        adapter.create_task(definition)

    assert captured.value.task_name == definition.task_name
    assert len(client.calls) == 1
    assert len(client.get_calls) == 1


def test_ambiguous_create_with_mismatched_task_is_conflict() -> None:
    client = FakeCloudTasksClient(TimeoutError("connection ended"))
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())
    existing = tasks_v2.Task(adapter._api_task(definition))
    existing.http_request.body = b'{"different":true}'
    client.get_effect = existing

    with pytest.raises(CloudTaskDefinitionConflictError) as captured:
        adapter.create_task(definition)

    assert captured.value.mismatched_fields == ("payload",)
    assert len(client.calls) == 1
    assert len(client.get_calls) == 1


def test_definite_api_rejection_is_typed_without_sensitive_detail() -> None:
    client = FakeCloudTasksClient(PermissionDenied("sensitive-provider-detail"))
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())

    with pytest.raises(CloudTaskCreateRejectedError) as captured:
        adapter.create_task(definition)

    assert captured.value.task_name == definition.task_name
    assert captured.value.error_type == "PermissionDenied"
    assert "sensitive-provider-detail" not in str(captured.value)
    assert len(client.calls) == 1


def test_unknown_transport_failure_is_ambiguous_and_not_retried() -> None:
    client = FakeCloudTasksClient(
        TimeoutError("sensitive-provider-detail"),
        NotFound("not found"),
    )
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())

    with pytest.raises(CloudTaskCreateAmbiguousError) as captured:
        adapter.create_task(definition)

    assert captured.value.task_name == definition.task_name
    assert captured.value.error_type == "TimeoutError"
    assert "sensitive-provider-detail" not in str(captured.value)
    assert len(client.calls) == 1
    assert len(client.get_calls) == 1


def test_malformed_success_response_is_ambiguous() -> None:
    client = FakeCloudTasksClient()
    adapter = GoogleCloudTasksAdapter(config(), client)
    definition = adapter.build_task(instruction())
    client.create_task = lambda request, retry: SimpleNamespace(name="wrong-name")  # type: ignore[assignment]
    client.get_effect = NotFound("not found")

    with pytest.raises(CloudTaskCreateAmbiguousError, match="could not be confirmed"):
        adapter.create_task(definition)


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": "bad/project"},
        {"location_id": "bad_location"},
        {"queue_id": "bad_queue"},
        {"execution_url": "http://not-secure.example/tasks"},
        {"execution_url": "https://user:password@example.test/tasks"},
        {"service_account_email": "caller@another-project.iam.gserviceaccount.com"},
        {"oidc_audience": "not-a-url"},
    ],
)
def test_cloud_tasks_configuration_fails_closed(overrides: dict[str, str]) -> None:
    values = {
        "project_id": "fpl-bot-test",
        "location_id": "europe-west2",
        "queue_id": "deadline-posts",
        "execution_url": "https://service.example.test/tasks",
        "service_account_email": "task-caller@fpl-bot-test.iam.gserviceaccount.com",
        "oidc_audience": None,
    }
    values.update(overrides)

    with pytest.raises(CloudTaskValidationError):
        CloudTasksConfig(**values)  # type: ignore[arg-type]


def test_custom_oidc_audience_is_preserved() -> None:
    custom = CloudTasksConfig(
        project_id="fpl-bot-test",
        location_id="europe-west2",
        queue_id="deadline-posts",
        execution_url="https://service.example.test/tasks",
        service_account_email="task-caller@fpl-bot-test.iam.gserviceaccount.com",
        oidc_audience="https://service.example.test",
    )
    client = FakeCloudTasksClient()
    adapter = GoogleCloudTasksAdapter(custom, client)

    adapter.create_task(adapter.build_task(instruction()))

    task = client.calls[0][0]["task"]
    assert task.http_request.oidc_token.audience == "https://service.example.test"
