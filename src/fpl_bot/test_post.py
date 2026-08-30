"""Explicit one-shot runner for a controlled Post to the dedicated X test account."""

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, TextIO

from fpl_bot.errors import FplBotError
from fpl_bot.x_api import CreatedXPost, XApiClient
from fpl_bot.x_config import XPostingConfig

TEST_POST_TEXT = "FPL Bot API integration test — TEST ACCOUNT ONLY"


class XPostCreator(Protocol):
    """Small injectable boundary used by the one-shot runner."""

    def create_text_post(self, text: str) -> CreatedXPost: ...


ClientFactory = Callable[[XPostingConfig], XPostCreator]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or explicitly send one controlled Post to the dedicated X test account."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="send exactly one test Post after all environment and account guards pass",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: ClientFactory = XApiClient,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)

    if not args.live:
        print("Dry run only; no X API request was made.", file=stdout)
        print("Controlled test Post text:", file=stdout)
        print(TEST_POST_TEXT, file=stdout)
        print("Use --live only for the approved dedicated test account check.", file=stdout)
        return 0

    try:
        config = XPostingConfig.from_environment(environ)
        config.require_posting_guards()
        created = client_factory(config).create_text_post(TEST_POST_TEXT)
    except (FplBotError, ValueError) as exc:
        print(f"Controlled X test Post failed: {exc}", file=stderr)
        return 1

    print("Controlled X test Post created.", file=stdout)
    print(f"X Post ID: {created.post_id}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
