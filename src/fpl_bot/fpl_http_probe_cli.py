"""CLI for the fixed, body-free FPL HTTP diagnostic matrix."""

import argparse
import json

from fpl_bot.fpl_http_probe import FplHttpMatrixProbe


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe fixed public FPL endpoints safely")
    parser.add_argument("--event-id", type=int, required=True)
    arguments = parser.parse_args()

    observations = FplHttpMatrixProbe().run(event_id=arguments.event_id)
    print(
        json.dumps(
            {
                "result": "http_matrix_complete",
                "observations": [observation.fields() for observation in observations],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
