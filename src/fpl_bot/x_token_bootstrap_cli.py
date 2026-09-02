"""Explicit ADC-backed CLI for the one-time X token-state bootstrap."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, GoogleCloudXTokenStateStore
from fpl_bot.x_token_bootstrap import (
    LocalDpapiTokenStateReader,
    bootstrap_x_token_state,
    default_repository_root,
    utc_now,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    from google.cloud import firestore_v1, secretmanager

    firestore = firestore_v1.Client(project=args.project_id, database=args.database_id)
    secrets = secretmanager.SecretManagerServiceClient()
    config = CloudXTokenStateStoreConfig(
        project_id=args.project_id,
        secret_id=args.secret_id,
        expected_user_id=args.expected_user_id,
        project_number=args.project_number,
    )
    store = GoogleCloudXTokenStateStore(
        config,
        firestore_client=firestore,
        secret_manager_client=secrets,
    )
    local_state = LocalDpapiTokenStateReader(repository_root=default_repository_root()).read(
        Path(args.token_file), expected_user_id=args.expected_user_id
    )
    result = bootstrap_x_token_state(local_state, store, now_utc=utc_now())
    version_number = result.initialization.secret_version_name.rsplit("/", 1)[1]
    print(
        json.dumps(
            {
                "access_token_status": result.access_token_status,
                "authority_revision": result.initialization.revision,
                "refresh_token_present": result.refresh_token_present,
                "scopes": list(result.scopes),
                "secret_version": version_number,
                "status": result.initialization.status,
                "token_type": result.token_type,
                "expires_at_utc": result.expires_at_utc.isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize the production X OAuth token store from one local DPAPI handoff"
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--project-number", required=True)
    parser.add_argument("--database-id", default="(default)")
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--token-file", required=True)
    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
