"""Deterministic Cloud Tasks definitions and the production API adapter."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from google.api_core.exceptions import (
    AlreadyExists,
    FailedPrecondition,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    Unauthenticated,
)
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from fpl_bot.deadline_revalidation import ScheduledDeadlineInstruction
from fpl_bot.errors import FplBotError
from fpl_bot.posting_state import PostingStateValidationError, require_utc

PAYLOAD_VERSION = 1
TASK_ID_DOMAIN = "fpl-deadline"
PREFLIGHT_TASK_ID_DOMAIN = "fpl-preflight"
PREFLIGHT_LEAD_TIME = timedelta(minutes=5)
TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,500}\Z")
RESOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9-]+\Z")
PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")
SERVICE_ACCOUNT_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")


class CloudTaskError(FplBotError):
    """Base class for task-definition and Cloud Tasks boundary failures."""


class CloudTaskValidationError(CloudTaskError):
    """Raised before create_task when task input or configuration is invalid."""


class CloudTaskCreateRejectedError(CloudTaskError):
    """Raised when Cloud Tasks definitely rejects a create request."""

    def __init__(self, task_name: str, error_type: str) -> None:
        super().__init__("Cloud Tasks definitely rejected the deterministic task creation")
        self.task_name = task_name
        self.error_type = error_type


class CloudTaskCreateAmbiguousError(CloudTaskError):
    """Raised when the deterministic task may have been created."""

    def __init__(self, task_name: str, error_type: str) -> None:
        super().__init__(
            "Cloud Task creation could not be confirmed; reconcile using the same task name"
        )
        self.task_name = task_name
        self.error_type = error_type


class CloudTaskNameReservedError(CloudTaskError):
    """Raised when de-duplication retains a name but no current task exists."""

    def __init__(self, task_name: str) -> None:
        super().__init__(
            "Cloud Task name is retained for de-duplication, but no current task was found"
        )
        self.task_name = task_name


class CloudTaskDefinitionConflictError(CloudTaskError):
    """Raised when an existing deterministic name has a different task definition."""

    def __init__(self, task_name: str, mismatched_fields: tuple[str, ...]) -> None:
        super().__init__(
            "Existing Cloud Task conflicts with the intended safety-critical definition"
        )
        self.task_name = task_name
        self.mismatched_fields = mismatched_fields


class CloudTaskReconciliationError(CloudTaskError):
    """Raised when a duplicate task name cannot be inspected safely."""

    def __init__(self, task_name: str, error_type: str) -> None:
        super().__init__(
            "Cloud Task identity exists or may exist, but read-only reconciliation failed"
        )
        self.task_name = task_name
        self.error_type = error_type


class CloudTaskCreateDisposition(StrEnum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    RECONCILED = "reconciled"


@dataclass(frozen=True, slots=True)
class CloudTasksConfig:
    project_id: str
    location_id: str
    queue_id: str
    execution_url: str
    service_account_email: str
    oidc_audience: str | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.project_id, PROJECT_ID_PATTERN, "GCP project ID", 128)
        _require_pattern(self.location_id, RESOURCE_ID_PATTERN, "Cloud Tasks location ID", 63)
        _require_pattern(self.queue_id, RESOURCE_ID_PATTERN, "Cloud Tasks queue ID", 100)
        _require_https_url(self.execution_url, "Execution URL")
        expected_suffix = f"@{self.project_id}.iam.gserviceaccount.com"
        service_account_id = (
            self.service_account_email.removesuffix(expected_suffix)
            if isinstance(self.service_account_email, str)
            else ""
        )
        if (
            not isinstance(self.service_account_email, str)
            or not self.service_account_email.endswith(expected_suffix)
            or SERVICE_ACCOUNT_ID_PATTERN.fullmatch(service_account_id) is None
        ):
            raise CloudTaskValidationError(
                "Task caller service account must belong to the configured GCP project"
            )
        if self.oidc_audience is not None:
            _require_https_url(self.oidc_audience, "OIDC audience")

    @property
    def queue_name(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location_id}/queues/{self.queue_id}"

    def task_name(self, task_id: str) -> str:
        _require_task_id(task_id)
        return f"{self.queue_name}/tasks/{task_id}"


@dataclass(frozen=True, slots=True)
class CloudTaskDefinition:
    task_id: str
    task_name: str
    schedule_time_utc: datetime
    payload: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require_task_id(self.task_id)
        expected_suffix = f"/tasks/{self.task_id}"
        if not isinstance(self.task_name, str) or not self.task_name.endswith(expected_suffix):
            raise CloudTaskValidationError("Cloud Task name does not match its deterministic ID")
        try:
            require_utc(self.schedule_time_utc, "Cloud Task schedule time")
        except PostingStateValidationError as exc:
            raise CloudTaskValidationError(str(exc)) from None
        if not isinstance(self.payload, bytes) or not self.payload:
            raise CloudTaskValidationError("Cloud Task payload must be non-empty bytes")


class CloudTaskBoundary(Protocol):
    def build_task(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> CloudTaskDefinition: ...

    def create_task(
        self,
        definition: CloudTaskDefinition,
    ) -> CloudTaskCreateDisposition: ...


class _CloudTasksClient(Protocol):
    def create_task(
        self,
        request: Any,
        *,
        retry: Any,
    ) -> Any: ...

    def get_task(
        self,
        request: Any,
        *,
        retry: Any,
    ) -> Any: ...


class GoogleCloudTasksAdapter:
    """Construct and create one named HTTP task without application-level retries."""

    def __init__(
        self,
        config: CloudTasksConfig,
        client: _CloudTasksClient | None = None,
    ) -> None:
        self._config = config
        self._client = client if client is not None else tasks_v2.CloudTasksClient()

    def build_task(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> CloudTaskDefinition:
        task_id = deterministic_task_id(instruction)
        return CloudTaskDefinition(
            task_id=task_id,
            task_name=self._config.task_name(task_id),
            schedule_time_utc=instruction.expected_deadline_utc,
            payload=serialize_instruction(instruction),
        )

    def create_task(
        self,
        definition: CloudTaskDefinition,
    ) -> CloudTaskCreateDisposition:
        try:
            task = self._api_task(definition)
        except CloudTaskError:
            raise
        except Exception as exc:
            raise CloudTaskValidationError(
                "Cloud Task request construction failed before create_task"
            ) from exc

        try:
            created = self._client.create_task(
                request={"parent": self._config.queue_name, "task": task},
                retry=None,
            )
        except AlreadyExists:
            return self._reconcile_duplicate(definition, task)
        except (
            InvalidArgument,
            PermissionDenied,
            Unauthenticated,
            FailedPrecondition,
            NotFound,
        ) as exc:
            raise CloudTaskCreateRejectedError(
                definition.task_name,
                type(exc).__name__,
            ) from exc
        except Exception as exc:
            return self._reconcile_ambiguous(definition, task, exc)

        if getattr(created, "name", None) != definition.task_name:
            return self._reconcile_ambiguous(
                definition,
                task,
                RuntimeError("MalformedCreateTaskResponse"),
            )
        return CloudTaskCreateDisposition.CREATED

    def _reconcile_duplicate(
        self,
        definition: CloudTaskDefinition,
        intended_task: tasks_v2.Task,
    ) -> CloudTaskCreateDisposition:
        try:
            existing = self._get_task(definition.task_name)
        except NotFound as exc:
            raise CloudTaskNameReservedError(definition.task_name) from exc
        except Exception as exc:
            raise CloudTaskReconciliationError(
                definition.task_name,
                type(exc).__name__,
            ) from exc
        _require_equivalent_task(existing, intended_task, definition.task_name)
        return CloudTaskCreateDisposition.ALREADY_EXISTS

    def _reconcile_ambiguous(
        self,
        definition: CloudTaskDefinition,
        intended_task: tasks_v2.Task,
        create_error: Exception,
    ) -> CloudTaskCreateDisposition:
        try:
            existing = self._get_task(definition.task_name)
        except NotFound:
            raise CloudTaskCreateAmbiguousError(
                definition.task_name,
                type(create_error).__name__,
            ) from create_error
        except Exception as lookup_error:
            raise CloudTaskReconciliationError(
                definition.task_name,
                type(lookup_error).__name__,
            ) from lookup_error
        _require_equivalent_task(existing, intended_task, definition.task_name)
        return CloudTaskCreateDisposition.RECONCILED

    def _get_task(self, task_name: str) -> tasks_v2.Task:
        return self._client.get_task(
            request=tasks_v2.GetTaskRequest(
                name=task_name,
                response_view=tasks_v2.Task.View.FULL,
            ),
            retry=None,
        )

    def _api_task(self, definition: CloudTaskDefinition) -> tasks_v2.Task:
        schedule_time = timestamp_pb2.Timestamp()
        schedule_time.FromDatetime(definition.schedule_time_utc)
        return tasks_v2.Task(
            name=definition.task_name,
            schedule_time=schedule_time,
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=self._config.execution_url,
                headers={"Content-Type": "application/json"},
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._config.service_account_email,
                    audience=self._config.oidc_audience or self._config.execution_url,
                ),
                body=definition.payload,
            ),
        )


class GooglePreflightCloudTasksAdapter(GoogleCloudTasksAdapter):
    """Reuse the safe Cloud Tasks transport for the distinct preflight task definition."""

    def build_task(
        self,
        instruction: ScheduledDeadlineInstruction,
    ) -> CloudTaskDefinition:
        task_id = deterministic_preflight_task_id(instruction)
        return CloudTaskDefinition(
            task_id=task_id,
            task_name=self._config.task_name(task_id),
            schedule_time_utc=instruction.expected_deadline_utc - PREFLIGHT_LEAD_TIME,
            payload=serialize_instruction(instruction),
        )


def deterministic_task_id(instruction: ScheduledDeadlineInstruction) -> str:
    return _deterministic_task_id(instruction, TASK_ID_DOMAIN, "fpl")


def deterministic_preflight_task_id(instruction: ScheduledDeadlineInstruction) -> str:
    return _deterministic_task_id(instruction, PREFLIGHT_TASK_ID_DOMAIN, "fpl-preflight")


def _deterministic_task_id(
    instruction: ScheduledDeadlineInstruction,
    identity_domain: str,
    prefix: str,
) -> str:
    _require_instruction(instruction)
    identity = (
        f"{identity_domain}|{instruction.expected_event_id}|"
        f"{_format_utc(instruction.expected_deadline_utc)}"
    ).encode("ascii")
    return f"{prefix}-{hashlib.sha256(identity).hexdigest()[:40]}"


def serialize_instruction(instruction: ScheduledDeadlineInstruction) -> bytes:
    _require_instruction(instruction)
    payload = {
        "expected_deadline_utc": _format_utc(instruction.expected_deadline_utc),
        "expected_event_id": instruction.expected_event_id,
        "version": PAYLOAD_VERSION,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_instruction_payload(payload: bytes) -> ScheduledDeadlineInstruction:
    """Strictly parse the versioned immutable instruction carried by Cloud Tasks."""
    if not isinstance(payload, bytes) or not payload:
        raise CloudTaskValidationError("Cloud Task payload must be non-empty bytes")
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(decoded, object_pairs_hook=_object_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, CloudTaskValidationError) as exc:
        raise CloudTaskValidationError("Cloud Task payload must be valid JSON") from exc

    expected_keys = {"version", "expected_event_id", "expected_deadline_utc"}
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise CloudTaskValidationError(
            "Cloud Task payload must contain exactly the supported instruction fields"
        )
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != PAYLOAD_VERSION:
        raise CloudTaskValidationError("Cloud Task payload version is unsupported")
    deadline_value = raw["expected_deadline_utc"]
    if not isinstance(deadline_value, str):
        raise CloudTaskValidationError("Cloud Task deadline must be a UTC timestamp string")
    try:
        deadline = datetime.fromisoformat(deadline_value.replace("Z", "+00:00"))
        require_utc(deadline, "Scheduled deadline")
        return ScheduledDeadlineInstruction(
            expected_event_id=raw["expected_event_id"],
            expected_deadline_utc=deadline,
        )
    except (ValueError, TypeError, PostingStateValidationError, FplBotError) as exc:
        raise CloudTaskValidationError("Cloud Task instruction identity is invalid") from exc


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CloudTaskValidationError("Cloud Task payload contains duplicate fields")
        result[key] = value
    return result


def _require_instruction(instruction: ScheduledDeadlineInstruction) -> None:
    if not isinstance(instruction, ScheduledDeadlineInstruction):
        raise CloudTaskValidationError("Cloud Task requires a ScheduledDeadlineInstruction")


def _format_utc(value: datetime) -> str:
    try:
        require_utc(value, "Scheduled deadline")
    except PostingStateValidationError as exc:
        raise CloudTaskValidationError(str(exc)) from None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise CloudTaskValidationError("Cloud Task ID contains unsupported characters or length")


def _require_pattern(value: str, pattern: re.Pattern[str], label: str, limit: int) -> None:
    if not isinstance(value, str) or len(value) > limit or pattern.fullmatch(value) is None:
        raise CloudTaskValidationError(f"{label} is invalid")


def _require_https_url(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise CloudTaskValidationError(f"{label} must be an HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CloudTaskValidationError(f"{label} must be an HTTPS URL without credentials")


def _require_equivalent_task(
    existing: tasks_v2.Task,
    intended: tasks_v2.Task,
    task_name: str,
) -> None:
    mismatched: list[str] = []
    if getattr(existing, "name", None) != task_name:
        mismatched.append("name")
    if _utc_datetime(getattr(existing, "schedule_time", None)) != _utc_datetime(
        intended.schedule_time
    ):
        mismatched.append("schedule_time")

    existing_request = getattr(existing, "http_request", None)
    intended_request = intended.http_request
    if existing_request is None or existing_request.http_method != intended_request.http_method:
        mismatched.append("http_method")
    if existing_request is None or existing_request.url != intended_request.url:
        mismatched.append("execution_url")
    if existing_request is None or bytes(existing_request.body) != bytes(intended_request.body):
        mismatched.append("payload")

    existing_oidc = getattr(existing_request, "oidc_token", None)
    intended_oidc = intended_request.oidc_token
    if (
        existing_oidc is None
        or existing_oidc.service_account_email != intended_oidc.service_account_email
    ):
        mismatched.append("oidc_service_account")
    if existing_oidc is None or existing_oidc.audience != intended_oidc.audience:
        mismatched.append("oidc_audience")

    if mismatched:
        raise CloudTaskDefinitionConflictError(task_name, tuple(mismatched))


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    if hasattr(value, "ToDatetime"):
        try:
            return value.ToDatetime(tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    return None
