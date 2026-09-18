"""Quiet-window read-only identity and existing-post audit. No create-post client."""

import argparse
import json
import sys
from typing import Any

from fpl_bot.cloud_token_store import CloudXTokenStateStoreConfig, GoogleCloudXTokenStateStore
from fpl_bot.runtime_config import XCloudRuntimeConfig
from fpl_bot.tweet import render_v1_tweet
from fpl_bot.x_api import UrllibXHttpTransport, XHttpRequest, XHttpResponse, XIdentityClient
from fpl_bot.x_errors import XConfigurationError, XResponseValidationError
from fpl_bot.x_oauth_verify import create_cloud_oauth_identity_verifier


def validate_existing_post(
    response: XHttpResponse, *, post_id: str, user_id: str, event_code: str
) -> dict[str, Any]:
    XIdentityClient._raise_for_read_status(response)
    try:
        data = json.loads(response.body)["data"]
        if (
            data["id"] != post_id
            or data["author_id"] != user_id
            or data["text"] != render_v1_tweet(event_code)
        ):
            raise ValueError
        if not isinstance(data["created_at"], str):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise XResponseValidationError("Existing post does not match reviewed audit") from None
    return {key: data[key] for key in ("id", "author_id", "text", "created_at")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post-id", required=True)
    parser.add_argument("--event-code", required=True)
    args = parser.parse_args()
    try:
        from google.cloud import firestore, secretmanager

        config = XCloudRuntimeConfig.from_environment()
        if (
            config.x_posting.posting_enabled
            or not args.post_id.isascii()
            or (not args.post_id.isdigit() or args.post_id.startswith("0"))
        ):
            raise XConfigurationError("Read-only audit configuration is invalid")
        expected_user = config.x_posting.require_configured_identity()
        store = GoogleCloudXTokenStateStore(
            CloudXTokenStateStoreConfig(
                config.gcp_project_id,
                config.x_token_secret_id,
                expected_user,
                project_number=config.gcp_project_number,
            ),
            firestore_client=firestore.Client(
                project=config.gcp_project_id, database=config.firestore_database_id
            ),
            secret_manager_client=secretmanager.SecretManagerServiceClient(),
        )
        # This is the existing shared coordinator: only refresh if actually needed.
        identity = create_cloud_oauth_identity_verifier(x_token_store=store).verify()
        current = store.read()
        response = UrllibXHttpTransport().send(
            XHttpRequest(
                "GET",
                f"https://api.x.com/2/tweets/{args.post_id}?tweet.fields=author_id,created_at",
                {
                    "Authorization": f"Bearer {current.state.access_token}",
                    "Accept": "application/json",
                    "User-Agent": "fpl-tweet-bot/0.2",
                },
            ),
            10.0,
        )
        post = validate_existing_post(
            response, post_id=args.post_id, user_id=expected_user, event_code=args.event_code
        )
    except Exception:
        print(
            json.dumps({"result": "read_only_probe_failed", "action": "inspect_authority"}),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "result": "read_only_verified",
                "x_user_id": identity.user_id,
                "credential_revision": current.revision,
                "existing_post": post,
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
