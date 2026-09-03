"""One-shot no-post test-account authorization from local DPAPI client credentials."""

import argparse
import sys
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fpl_bot.errors import FplBotError
from fpl_bot.x_api import AuthenticatedXUser
from fpl_bot.x_client_secret_capture import load_client_secret
from fpl_bot.x_config import X_ID_PATTERN
from fpl_bot.x_errors import XOAuthCallbackError, XOAuthConfigurationError
from fpl_bot.x_oauth import (
    OAUTH_SCOPES,
    ExclusiveTokenFileHandoff,
    LocalOAuthConfig,
    OAuthClientCredentials,
    OAuthTokenBundle,
    OAuthTokenHandoff,
    authorize_test_account,
)
from fpl_bot.x_oauth_callback import LoopbackOAuthCallbackReceiver


@dataclass(frozen=True, slots=True)
class ReauthorizationMetadata:
    user_id: str
    scopes: tuple[str, ...]
    refresh_token_present: bool
    token_type: str
    expires_at_utc: str
    handoff_saved: bool


class _MetadataHandoff(OAuthTokenHandoff):
    def __init__(self, delegate: OAuthTokenHandoff) -> None:
        self._delegate = delegate
        self.metadata: ReauthorizationMetadata | None = None

    def store(self, tokens: OAuthTokenBundle, user: AuthenticatedXUser) -> None:
        self._delegate.store(tokens, user)
        expires_at = tokens.received_at_utc + timedelta(seconds=tokens.expires_in_seconds)
        self.metadata = ReauthorizationMetadata(
            user_id=user.user_id,
            scopes=tokens.scopes,
            refresh_token_present=bool(tokens.refresh_token),
            token_type=tokens.token_type,
            expires_at_utc=expires_at.isoformat().replace("+00:00", "Z"),
            handoff_saved=True,
        )


def authorize_from_local_credentials(
    *,
    client_id_path: Path,
    encrypted_client_secret_path: Path,
    expected_user_id: str,
    token_output_path: Path,
    repository_root: Path,
    authorizer: Callable[..., AuthenticatedXUser] = authorize_test_account,
    browser_open: Callable[[str], bool] | None = None,
) -> ReauthorizationMetadata:
    if not X_ID_PATTERN.fullmatch(expected_user_id):
        raise XOAuthConfigurationError("Expected X user ID must be a positive numeric value")
    if token_output_path.exists() or token_output_path.is_symlink():
        raise XOAuthConfigurationError("The new token-output file already exists")
    try:
        client_id = client_id_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise XOAuthConfigurationError("The retained OAuth Client ID could not be read") from exc
    if not client_id or not client_id.isprintable():
        raise XOAuthConfigurationError("The retained OAuth Client ID is invalid")

    client_secret = load_client_secret(encrypted_client_secret_path)
    config = LocalOAuthConfig(
        credentials=OAuthClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
        ),
        token_output_file=token_output_path,
        expected_user_id=expected_user_id,
    )
    handoff = _MetadataHandoff(
        ExclusiveTokenFileHandoff(token_output_path, repository_root=repository_root)
    )
    try:
        authorizer(
            config,
            receiver=LoopbackOAuthCallbackReceiver(),
            handoff=handoff,
            browser_open=browser_open or _open_browser,
        )
    finally:
        client_secret = ""
    if handoff.metadata is None:
        raise XOAuthConfigurationError("The fresh encrypted token handoff was not saved")
    return handoff.metadata


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize the expected X test account without posting"
    )
    parser.add_argument("--client-id-path", required=True, type=Path)
    parser.add_argument("--encrypted-client-secret-path", required=True, type=Path)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--token-output-path", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        metadata = authorize_from_local_credentials(
            client_id_path=arguments.client_id_path,
            encrypted_client_secret_path=arguments.encrypted_client_secret_path,
            expected_user_id=arguments.expected_user_id,
            token_output_path=arguments.token_output_path,
            repository_root=arguments.repository_root,
        )
    except (FplBotError, OSError):
        print("authorization_succeeded=false", file=sys.stderr)
        return 1

    print("authorization_succeeded=true")
    print(f"verified_user_id={metadata.user_id}")
    print(f"required_scopes_present={str(set(metadata.scopes) == set(OAUTH_SCOPES)).lower()}")
    print(f"refresh_token_present={str(metadata.refresh_token_present).lower()}")
    print(f"token_type={metadata.token_type}")
    print(f"access_token_expires_at_utc={metadata.expires_at_utc}")
    print(f"new_dpapi_handoff_saved={str(metadata.handoff_saved).lower()}")
    return 0


def _open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url, new=1, autoraise=True)
    except webbrowser.Error as exc:
        raise XOAuthCallbackError("Could not open the X authorization page") from exc


if __name__ == "__main__":
    raise SystemExit(main())
