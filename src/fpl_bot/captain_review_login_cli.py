"""Open only the dedicated Captain browser profile for manual FPL Review login."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from fpl_bot.captain_review_browser import open_manual_review_login
from fpl_bot.errors import CaptainReviewBrowserError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open stable Google Chrome with a dedicated FPL Review profile."
    )
    parser.add_argument(
        "--profile-dir",
        required=True,
        type=Path,
        help="Dedicated profile directory outside the repository and ordinary browser profile",
    )
    args = parser.parse_args(argv)
    try:
        open_manual_review_login(args.profile_dir)
    except CaptainReviewBrowserError as exc:
        print(f"FPL Review login setup failed: {exc.category}", file=sys.stderr)
        return 1
    print("Dedicated stable-Chrome profile closed; authentication will be checked by the dry run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
