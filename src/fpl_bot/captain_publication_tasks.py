"""Deterministic Cloud Tasks routing from accepted Captain evidence to its publisher."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid5

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

from fpl_bot.captain_cloud_tasks import CaptainTaskProviderError
from fpl_bot.captain_orchestration_timing import require_utc
from fpl_bot.captain_publication import PublicationInstruction
from fpl_bot.captain_state import (
    REHEARSAL_DESTINATION_USER_ID,
    StateConflict,
    ValidatedCandidateRecord,
)
from fpl_bot.production_x_identity import PRODUCTION_X_USER_ID

PUBLICATION_NAMESPACE = UUID("00d8f157-1a67-4819-a957-2ce4b745ec58")


@dataclass(frozen=True, slots=True)
class PublicationTaskConfirmation:
    identity: str
    digest: str


@dataclass(frozen=True, slots=True)
class CaptainPublicationTasksConfig:
    project: str
    location: str
    queue: str
    publisher_origin: str
    oidc_service_account: str
    oidc_audience: str
    destination_user_id: str

    def __post_init__(self) -> None:
        for value in (self.project, self.location, self.queue):
            if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", value):
                raise ValueError("invalid Captain publication task resource")
        for value in (self.publisher_origin, self.oidc_audience):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("publisher origin must be credential-free HTTPS")
        if self.publisher_origin != self.oidc_audience:
            raise ValueError("publisher audience must equal its immutable origin")
        if not self.publisher_origin.startswith("https://captain-production-publisher-"):
            raise ValueError("production publisher origin required")
        if not self.oidc_service_account.startswith("captain-prod-pub-invoker@"):
            raise ValueError("dedicated production publisher invoker required")
        if self.destination_user_id != PRODUCTION_X_USER_ID:
            raise ValueError("production Captain destination is immutable")

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.location}/queues/{self.queue}"


def instruction_for_candidate(candidate: ValidatedCandidateRecord) -> PublicationInstruction:
    """Derive a stable publisher instruction from immutable persisted evidence only."""

    if not isinstance(candidate, ValidatedCandidateRecord):
        raise StateConflict("validated Captain candidate required")
    if candidate.key.destination_user_id == REHEARSAL_DESTINATION_USER_ID:
        raise StateConflict("non-postable rehearsal cannot be routed to a publisher")
    claim_id = uuid5(
        PUBLICATION_NAMESPACE,
        ":".join(
            (
                candidate.key.destination_user_id,
                str(candidate.key.event_id),
                str(candidate.generation_id),
                candidate.candidate_digest,
            )
        ),
    )
    return PublicationInstruction(
        candidate.generation_id,
        candidate.attempt_id,
        candidate.handoff_digest,
        candidate.candidate_digest,
        claim_id,
    )


def publication_task_identity(instruction: PublicationInstruction) -> str:
    if not isinstance(instruction, PublicationInstruction):
        raise StateConflict("invalid Captain publication instruction")
    return f"captain-x-{instruction.generation_id}"


def publication_task_digest(instruction: PublicationInstruction) -> str:
    return hashlib.sha256(
        json.dumps(instruction.to_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CaptainPublicationTasksAdapter:
    """Create or reconcile one exact authenticated publisher delivery."""

    def __init__(self, config: CaptainPublicationTasksConfig, client=None) -> None:
        self.config = config
        self.destination_user_id = config.destination_user_id
        self.client = client or tasks_v2.CloudTasksClient()

    def _api_task(self, instruction: PublicationInstruction, scheduled_at: datetime):
        require_utc(scheduled_at)
        stamp = timestamp_pb2.Timestamp()
        stamp.FromDatetime(scheduled_at)
        return tasks_v2.Task(
            name=f"{self.config.parent}/tasks/{publication_task_identity(instruction)}",
            schedule_time=stamp,
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=(self.config.publisher_origin.rstrip("/") + "/captain/publisher/execute"),
                headers={"Content-Type": "application/json"},
                body=json.dumps(
                    instruction.to_payload(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self.config.oidc_service_account,
                    audience=self.config.oidc_audience,
                ),
            ),
        )

    @staticmethod
    def _equivalent(actual, expected) -> bool:
        actual_request, expected_request = actual.http_request, expected.http_request
        return (
            actual.name == expected.name
            and actual.schedule_time == expected.schedule_time
            and actual_request.http_method == expected_request.http_method
            and actual_request.url == expected_request.url
            and bytes(actual_request.body) == bytes(expected_request.body)
            and actual_request.oidc_token.service_account_email
            == expected_request.oidc_token.service_account_email
            and actual_request.oidc_token.audience == expected_request.oidc_token.audience
        )

    def _lookup(self, intended):
        return self.client.get_task(
            request=tasks_v2.GetTaskRequest(
                name=intended.name, response_view=tasks_v2.Task.View.FULL
            ),
            retry=None,
        )

    def ensure(
        self, instruction: PublicationInstruction, scheduled_at: datetime
    ) -> PublicationTaskConfirmation:
        intended = self._api_task(instruction, scheduled_at)
        identity = publication_task_identity(instruction)
        digest = publication_task_digest(instruction)
        try:
            created = self.client.create_task(
                request={"parent": self.config.parent, "task": intended}, retry=None
            )
        except AlreadyExists:
            try:
                actual = self._lookup(intended)
            except Exception as exc:
                raise CaptainTaskProviderError(
                    "publisher_task_reconciliation_failed", identity
                ) from exc
            if not self._equivalent(actual, intended):
                raise StateConflict("deterministic publisher task content conflict") from None
        except (InvalidArgument, PermissionDenied, Unauthenticated, FailedPrecondition) as exc:
            raise CaptainTaskProviderError("publisher_task_create_rejected", identity) from exc
        except Exception as exc:
            try:
                actual = self._lookup(intended)
            except NotFound:
                raise CaptainTaskProviderError("publisher_task_create_ambiguous", identity) from exc
            except Exception as lookup:
                raise CaptainTaskProviderError(
                    "publisher_task_reconciliation_failed", identity
                ) from lookup
            if not self._equivalent(actual, intended):
                raise StateConflict("deterministic publisher task content conflict") from None
        else:
            if getattr(created, "name", None) != intended.name:
                raise CaptainTaskProviderError("publisher_task_create_ambiguous", identity)
        return PublicationTaskConfirmation(identity, digest)
