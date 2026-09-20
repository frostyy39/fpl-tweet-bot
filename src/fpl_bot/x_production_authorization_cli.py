"""One-shot no-post discovery and authorization of the intended production X account."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from fpl_bot.errors import FplBotError
from fpl_bot.x_oauth import OAUTH_SCOPES
from fpl_bot.x_reauthorization_cli import authorize_from_local_credentials


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize and identify one production X account without posting"
    )
    parser.add_argument("--client-id-path", required=True, type=Path)
    parser.add_argument("--encrypted-client-secret-path", required=True, type=Path)
    parser.add_argument("--token-output-path", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        metadata = authorize_from_local_credentials(
            client_id_path=arguments.client_id_path,
            encrypted_client_secret_path=arguments.encrypted_client_secret_path,
            expected_user_id=None,
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
