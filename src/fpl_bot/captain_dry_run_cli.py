"""Print one real-data Captain dry run locally; this module cannot post."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from fpl_bot.api import FplApiClient
from fpl_bot.captain_dry_run import build_live_captain_report, render_captain_audit
from fpl_bot.captain_projection import FplReviewCsvProjectionSource
from fpl_bot.errors import FplBotError


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_output()
    parser = argparse.ArgumentParser(
        description="Render Captain picks from live FPL data and a local FPL Review CSV."
    )
    parser.add_argument(
        "--projections-csv",
        required=True,
        type=Path,
        help="Path to a manually exported FPL Review projections CSV",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="FPL HTTP timeout in seconds (default: 10)",
    )
    args = parser.parse_args(argv)

    try:
        report = build_live_captain_report(
            FplApiClient(timeout_seconds=args.timeout),
            FplReviewCsvProjectionSource(args.projections_csv),
        )
    except FplBotError as exc:
        print(f"Captain dry run failed: {exc}", file=sys.stderr)
        return 1

    print(render_captain_audit(report))
    return 0


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _configure_utf8_output() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
