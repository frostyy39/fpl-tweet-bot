"""Explicit ADC-backed CLI for a reviewed X OAuth token-state reseed."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, GoogleCloudXTokenStateStore
from fpl_bot.x_token_bootstrap import LocalDpapiTokenStateReader, default_repository_root
from fpl_bot.x_token_reseed import reseed_x_token_state


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
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
        result = reseed_x_token_state(
            local_state,
            store,
            expected_revision=args.expected_revision,
        )
    except Exception:
        print(json.dumps({"result": "reseed_failed"}), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "authority_revision": result.authoritative_revision,
                "previous_revision": result.previous_revision,
                "result": result.status,
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replace one reviewed X OAuth token generation without refresh or X access"
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--project-number", required=True)
    parser.add_argument("--database-id", default="(default)")
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--token-file", required=True)
    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
