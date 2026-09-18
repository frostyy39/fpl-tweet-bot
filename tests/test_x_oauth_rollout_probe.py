import json

import pytest

from fpl_bot.tweet import render_v1_tweet
from fpl_bot.x_api import XHttpResponse
from fpl_bot.x_errors import XResponseValidationError
from fpl_bot.x_oauth_rollout_probe import validate_existing_post

POST = {
    "id": "123",
    "author_id": "456",
    "text": render_v1_tweet("GW5"),
    "created_at": "2026-09-18T17:30:03.000Z",
}


def validate(data):
    return validate_existing_post(
        XHttpResponse(200, json.dumps({"data": data}).encode()),
        post_id="123",
        user_id="456",
        event_code="GW5",
    )


def test_exact_read_only_post_audit():
    assert validate(POST) == POST


@pytest.mark.parametrize(
    "field,value", [("id", "789"), ("author_id", "789"), ("text", "wrong"), ("created_at", None)]
)
def test_mismatched_audit_fails_closed(field, value):
    with pytest.raises(XResponseValidationError):
        validate({**POST, field: value})


def test_invalid_json_never_exposes_response_body():
    with pytest.raises(XResponseValidationError, match="reviewed audit") as error:
        validate_existing_post(
            XHttpResponse(200, b"synthetic-secret"), post_id="123", user_id="456", event_code="GW5"
        )
    assert "synthetic-secret" not in str(error.value)
