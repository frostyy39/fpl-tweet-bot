"""Non-interactive entry point for one safe cloud OAuth identity verification."""

import json
import sys

from fpl_bot.x_oauth_verify import create_cloud_oauth_identity_verifier


def main() -> int:
    try:
        result = create_cloud_oauth_identity_verifier().verify()
    except Exception:
        print(json.dumps({"result": "verification_failed"}), file=sys.stderr)
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
