"""Fresh official-FPL target check used immediately before production arming."""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from fpl_bot.api import FplApiClient
from fpl_bot.captain_orchestration_timing import require_utc, utc_text
from fpl_bot.service import FplDataSource, build_next_event_report


class ProductionTargetMismatch(RuntimeError):
    """The live authoritative target differs from the separately reviewed target."""


@dataclass(frozen=True, slots=True)
class ProductionTarget:
    event_id: int
    event_code: str
    official_deadline_utc: datetime
    captain_warmup_utc: datetime
    captain_target_utc: datetime
    captain_expiry_utc: datetime

    def payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_code": self.event_code,
            "official_deadline_utc": utc_text(self.official_deadline_utc),
            "captain_warmup_utc": utc_text(self.captain_warmup_utc),
            "captain_target_utc": utc_text(self.captain_target_utc),
            "captain_expiry_utc": utc_text(self.captain_expiry_utc),
        }


def verify_production_target(
    source: FplDataSource,
    *,
    expected_event_id: int,
    expected_deadline_utc: datetime,
    now: datetime | None = None,
) -> ProductionTarget:
    require_utc(expected_deadline_utc)
    report = build_next_event_report(source, now=now)
    if (
        report.event.event_id != expected_event_id
        or report.event.deadline_utc != expected_deadline_utc
    ):
        raise ProductionTargetMismatch(
            "fresh official FPL event/deadline differs from the reviewed production target"
        )
    target = report.event.deadline_utc - timedelta(hours=2)
    return ProductionTarget(
        report.event.event_id,
        report.event_code,
        report.event.deadline_utc,
        target - timedelta(minutes=15),
        target,
        target + timedelta(minutes=5),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-event-id", required=True, type=int)
    parser.add_argument("--expected-deadline-utc", required=True)
    args = parser.parse_args(argv)
    try:
        deadline = datetime.fromisoformat(args.expected_deadline_utc.replace("Z", "+00:00"))
        target = verify_production_target(
            FplApiClient(),
            expected_event_id=args.expected_event_id,
            expected_deadline_utc=deadline,
        )
    except (ValueError, ProductionTargetMismatch):
        print(json.dumps({"status": "stale_or_invalid_target"}, sort_keys=True))
        return 1
    print(json.dumps({"status": "target_confirmed", **target.payload()}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
