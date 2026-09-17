"""Pure Captain Cloud Tasks delivery decoding/dispatch; no provider calls or X path."""

from dataclasses import dataclass
from datetime import datetime

from fpl_bot.captain_controller import CaptainController, DeliveryResult, TaskEnvelope
from fpl_bot.captain_handoff import CaptainAssignment
from fpl_bot.captain_orchestration_timing import require_utc, utc_text
from fpl_bot.captain_state import StateConflict, TaskKind


@dataclass(frozen=True, slots=True)
class TaskDeliveryOutcome:
    status: str
    retry_at_utc: datetime | None = None

    def json_body(self) -> dict:
        return {
            "status": self.status,
            "retry_at": utc_text(self.retry_at_utc) if self.retry_at_utc else None,
        }


def decode_task_payload(payload: object) -> TaskEnvelope:
    if (
        type(payload) is not dict
        or set(payload)
        != {
            "version",
            "identity",
            "digest",
            "destination_user_id",
            "assignment",
            "kind",
            "scheduled_at",
        }
        or payload["version"] != 1
    ):
        raise StateConflict("malformed Captain task envelope")
    try:
        scheduled = datetime.fromisoformat(payload["scheduled_at"].replace("Z", "+00:00"))
        require_utc(scheduled)
        envelope = TaskEnvelope(
            payload["destination_user_id"],
            CaptainAssignment.from_payload(payload["assignment"]),
            TaskKind(payload["kind"]),
            scheduled,
        )
    except (TypeError, ValueError) as exc:
        raise StateConflict("malformed Captain task envelope") from exc
    if payload["identity"] != envelope.identity or payload["digest"] != envelope.digest:
        raise StateConflict("Captain task envelope identity conflict")
    return envelope


def handle_task_delivery(controller: CaptainController, payload: object) -> TaskDeliveryOutcome:
    result: DeliveryResult = controller.deliver(decode_task_payload(payload))
    return TaskDeliveryOutcome(result.status, result.retry_at)
