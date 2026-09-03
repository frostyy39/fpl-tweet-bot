"""Non-interactive CLI for one structurally read-only FPL cloud probe."""

import json
import sys

from fpl_bot.fpl_diagnostics import diagnose_fpl_failure
from fpl_bot.fpl_probe import create_fpl_probe


def main() -> int:
    try:
        result = create_fpl_probe().run()
    except Exception as error:
        payload: dict[str, str | int] = {"result": "probe_failed"}
        payload.update(diagnose_fpl_failure(error).fields())
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "result": "success",
                "selected_event_id": result.event.event_id,
                "official_deadline_utc": result.event.deadline_utc.isoformat(),
                "deadline_london": result.deadline_london.isoformat(),
                "deadline_is_today_london": result.is_current_london_day,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
