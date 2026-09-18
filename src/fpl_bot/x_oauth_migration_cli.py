"""Explicit operator-only authority migration, with no token/Secret Manager API."""

import argparse
import json
import sys
from datetime import datetime

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig
from fpl_bot.x_oauth_migration import LegacyAuthorityExpectation, migrate_legacy_authority


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--previous-version")
    parser.add_argument("--authority-updated-at", required=True)
    parser.add_argument("--consumers-quiesced", action="store_true")
    parser.add_argument("--legacy-history-reviewed", action="store_true")
    args = parser.parse_args()
    try:
        from google.cloud import firestore

        config = CloudXTokenStateStoreConfig(args.project, args.secret_id, args.expected_user_id)
        expectation = LegacyAuthorityExpectation(
            args.revision,
            args.version,
            args.previous_version,
            datetime.fromisoformat(args.authority_updated_at.replace("Z", "+00:00")),
        )
        result = migrate_legacy_authority(
            config,
            expectation,
            firestore_client=firestore.Client(project=args.project, database=args.database),
            consumers_quiesced=args.consumers_quiesced,
            legacy_history_reviewed=args.legacy_history_reviewed,
        )
    except Exception:
        print(
            json.dumps({"result": "migration_unconfirmed", "action": "inspect_metadata"}),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "result": result,
                "schema_version": 2,
                "revision": expectation.revision,
                "secret_version_name": expectation.secret_version_name,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
