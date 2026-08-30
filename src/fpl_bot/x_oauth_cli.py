"""Local X OAuth helper; it has no Post-creation capability."""

import sys
import webbrowser
from pathlib import Path

from fpl_bot.errors import FplBotError
from fpl_bot.x_errors import XOAuthCallbackError, XOAuthConfigurationError
from fpl_bot.x_oauth import (
    ExclusiveTokenFileHandoff,
    LocalOAuthConfig,
    authorize_test_account,
)
from fpl_bot.x_oauth_callback import LoopbackOAuthCallbackReceiver


def render_authorization_success(user_id: str, username: str) -> str:
    return "\n".join(
        (
            f"Authenticated X user ID: {user_id}",
            f"Authenticated X username: {username}",
            "OAuth tokens stored in the configured external handoff file.",
            "Posting remains disabled.",
        )
    )


def _open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url, new=1, autoraise=True)
    except webbrowser.Error as exc:
        raise XOAuthCallbackError("Could not open the X authorization page") from exc


def _find_repository_root() -> Path:
    package_candidate = Path(__file__).resolve().parents[2]
    if (package_candidate / ".git").exists():
        return package_candidate
    current_directory = Path.cwd().resolve()
    for candidate in (current_directory, *current_directory.parents):
        if (candidate / ".git").exists():
            return candidate
    raise XOAuthConfigurationError("Run the local OAuth helper from this repository checkout")


def main() -> int:
    try:
        config = LocalOAuthConfig.from_environment()
        user = authorize_test_account(
            config,
            receiver=LoopbackOAuthCallbackReceiver(),
            handoff=ExclusiveTokenFileHandoff(
                config.token_output_file,
                repository_root=_find_repository_root(),
            ),
            browser_open=_open_browser,
        )
    except (FplBotError, OSError) as exc:
        print(f"X authorization failed: {exc}", file=sys.stderr)
        return 1

    print(render_authorization_success(user.user_id, user.username))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
