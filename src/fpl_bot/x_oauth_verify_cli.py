"""Non-interactive entry point for one safe cloud OAuth identity verification."""

import json
import sys

from fpl_bot.x_oauth_verify import (
    create_cloud_oauth_identity_verifier,
    diagnose_verification_failure,
)


def main() -> int:
    try:
        result = create_cloud_oauth_identity_verifier().verify()
    except Exception as error:
        diagnostic = diagnose_verification_failure(error)
        print(json.dumps(diagnostic.as_payload(), sort_keys=True), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"result": "identity_verified", "x_user_id": result.user_id},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
