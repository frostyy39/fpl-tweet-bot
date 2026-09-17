"""Captain-only Cloud Tasks adapter for the durable outbox port."""

import json
import re
from dataclasses import dataclass
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

from fpl_bot.captain_controller import TaskConfirmation, TaskEnvelope
from fpl_bot.captain_orchestration_timing import utc_text
from fpl_bot.captain_state import StateConflict


class CaptainTaskProviderError(RuntimeError):
    def __init__(self, category: str, identity: str):
        self.category, self.identity = category, identity
        super().__init__(f"{category}: {identity}")


@dataclass(frozen=True, slots=True)
class CaptainCloudTasksConfig:
    project: str
    location: str
    queue: str
    controller_origin: str
    oidc_service_account: str
    oidc_audience: str

    def __post_init__(self) -> None:
        for value in (self.project, self.location, self.queue):
            if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", value):
                raise ValueError("invalid Captain Cloud Tasks resource")
        for value in (self.controller_origin, self.oidc_audience):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Captain controller URLs must be credential-free HTTPS")
        if urlsplit(self.controller_origin).path not in {"", "/"}:
            raise ValueError("controller origin must not contain a route")
        if "good-luck" in (self.queue + self.controller_origin).casefold():
            raise ValueError("Captain adapter cannot target Good Luck resources")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.gserviceaccount\.com", self.oidc_service_account):
            raise ValueError("invalid OIDC service account")

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.location}/queues/{self.queue}"


def task_payload(task: TaskEnvelope) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "identity": task.identity,
            "digest": task.digest,
            "destination_user_id": task.destination_user_id,
            "assignment": task.assignment.to_payload(),
            "kind": task.kind.value,
            "scheduled_at": utc_text(task.scheduled_at),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class CaptainCloudTasksAdapter:
    """Create/confirm a deterministic task; never retries a provider mutation itself."""

    def __init__(self, config: CaptainCloudTasksConfig, client=None):
        self.config = config
        self.client = client or tasks_v2.CloudTasksClient()

    def _api_task(self, task: TaskEnvelope):
        stamp = timestamp_pb2.Timestamp()
        stamp.FromDatetime(task.scheduled_at)
        name = f"{self.config.parent}/tasks/{task.identity}"
        route = f"/captain/tasks/{task.kind.value}"
        return tasks_v2.Task(
            name=name,
            schedule_time=stamp,
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=self.config.controller_origin.rstrip("/") + route,
                headers={"Content-Type": "application/json"},
                body=task_payload(task),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self.config.oidc_service_account,
                    audience=self.config.oidc_audience,
                ),
            ),
        )

    @staticmethod
    def _equivalent(actual, expected) -> bool:
        ar, er = actual.http_request, expected.http_request
        return (
            actual.name == expected.name
            and actual.schedule_time == expected.schedule_time
            and ar.http_method == er.http_method
            and ar.url == er.url
            and bytes(ar.body) == bytes(er.body)
            and ar.oidc_token.service_account_email == er.oidc_token.service_account_email
            and ar.oidc_token.audience == er.oidc_token.audience
        )

    def _lookup(self, intended):
        return self.client.get_task(
            request=tasks_v2.GetTaskRequest(
                name=intended.name, response_view=tasks_v2.Task.View.FULL
            ),
            retry=None,
        )

    def ensure(self, task: TaskEnvelope) -> TaskConfirmation:
        if not isinstance(task, TaskEnvelope):
            raise StateConflict("invalid Captain task")
        intended = self._api_task(task)
        try:
            created = self.client.create_task(
                request={"parent": self.config.parent, "task": intended}, retry=None
            )
        except AlreadyExists:
            try:
                actual = self._lookup(intended)
            except Exception as exc:
                raise CaptainTaskProviderError("task_reconciliation_failed", task.identity) from exc
            if not self._equivalent(actual, intended):
                raise StateConflict("deterministic Captain task content conflict") from None
        except (InvalidArgument, PermissionDenied, Unauthenticated, FailedPrecondition) as exc:
            raise CaptainTaskProviderError("task_create_rejected", task.identity) from exc
        except Exception as exc:
            try:
                actual = self._lookup(intended)
            except NotFound:
                raise CaptainTaskProviderError("task_create_ambiguous", task.identity) from exc
            except Exception as lookup:
                raise CaptainTaskProviderError(
                    "task_reconciliation_failed", task.identity
                ) from lookup
            if not self._equivalent(actual, intended):
                raise StateConflict("deterministic Captain task content conflict") from None
        else:
            if getattr(created, "name", None) != intended.name:
                raise CaptainTaskProviderError("task_create_ambiguous", task.identity)
        return TaskConfirmation(task.identity, task.digest)
