import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_bot.cloud_token_store import serialize_token_state
from fpl_bot.x_errors import XTokenStateError, XTokenStoreError
from fpl_bot.x_token_bootstrap import ValidatedLocalTokenState
from fpl_bot.x_token_refresh import VersionedXTokenState, XOAuthTokenState
from fpl_bot.x_token_reseed import (
    XTokenReseedStatus,
    reseed_x_token_state,
)
from fpl_bot.x_token_reseed_cli import main

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
USER_ID = "123456789"
OLD_ACCESS = "old-access-value"
OLD_REFRESH = "old-refresh-value"
NEW_ACCESS = "new-access-value"
NEW_REFRESH = "new-refresh-value"


def token_state(access: str, refresh: str) -> XOAuthTokenState:
    return XOAuthTokenState(
        access_token=access,
        refresh_token=refresh,
        expires_at_utc=NOW + timedelta(hours=2),
    )


def validated_new_state() -> ValidatedLocalTokenState:
    return ValidatedLocalTokenState(USER_ID, token_state(NEW_ACCESS, NEW_REFRESH))


class FakeStore:
    def __init__(self) -> None:
        self.current = VersionedXTokenState("1", token_state(OLD_ACCESS, OLD_REFRESH))
        self.replace_calls = 0
        self.replacement: XOAuthTokenState | None = None
        self.mode = "success"

    def read(self) -> VersionedXTokenState:
        return self.current

    def reseed_if_revision(
        self,
        expected_revision: str,
        replacement: XOAuthTokenState,
    ) -> bool:
        self.replace_calls += 1
        self.replacement = replacement
        if self.mode == "success":
            assert expected_revision == self.current.revision
            self.current = VersionedXTokenState("2", replacement)
            return True
        if self.mode == "reconciled":
            self.current = VersionedXTokenState("2", replacement)
            return False
        if self.mode == "lost":
            self.current = VersionedXTokenState(
                "2", token_state("other-access-value", "other-refresh-value")
            )
            return False
        if self.mode == "invalid":
            return None  # type: ignore[return-value]
        raise AssertionError("unexpected fake mode")


def test_reseed_replaces_one_exact_revision_and_verifies_authority() -> None:
    store = FakeStore()

    result = reseed_x_token_state(validated_new_state(), store, expected_revision="1")

    assert result.status is XTokenReseedStatus.RESEEDED
    assert result.previous_revision == "1"
    assert result.authoritative_revision == "2"
    assert store.replace_calls == 1
    assert serialize_token_state(store.current.state) == serialize_token_state(
        validated_new_state().state
    )


def test_rerun_against_authoritative_state_creates_no_generation() -> None:
    store = FakeStore()
    store.current = VersionedXTokenState("2", validated_new_state().state)

    result = reseed_x_token_state(validated_new_state(), store, expected_revision="1")

    assert result.status is XTokenReseedStatus.ALREADY_AUTHORITATIVE
    assert result.authoritative_revision == "2"
    assert store.replace_calls == 0


def test_stale_expected_revision_fails_before_replacement() -> None:
    store = FakeStore()
    store.current = VersionedXTokenState(
        "2", token_state("other-access-value", "other-refresh-value")
    )

    with pytest.raises(XTokenStoreError, match="no longer authoritative"):
        reseed_x_token_state(validated_new_state(), store, expected_revision="1")

    assert store.replace_calls == 0


def test_false_cas_reconciles_only_the_exact_new_authority() -> None:
    store = FakeStore()
    store.mode = "reconciled"

    result = reseed_x_token_state(validated_new_state(), store, expected_revision="1")

    assert result.status is XTokenReseedStatus.RECONCILED
    assert result.authoritative_revision == "2"
    assert store.replace_calls == 1


def test_false_cas_to_different_generation_fails_closed() -> None:
    store = FakeStore()
    store.mode = "lost"

    with pytest.raises(XTokenStoreError, match="lost its authority"):
        reseed_x_token_state(validated_new_state(), store, expected_revision="1")

    assert store.replace_calls == 1


@pytest.mark.parametrize("revision", ["", "0", "-1", "01", "revision", None])
def test_reseed_requires_positive_canonical_revision(revision: object) -> None:
    with pytest.raises(XTokenStateError):
        reseed_x_token_state(
            validated_new_state(),
            FakeStore(),
            expected_revision=revision,  # type: ignore[arg-type]
        )


def test_invalid_store_result_fails_closed() -> None:
    store = FakeStore()
    store.mode = "invalid"

    with pytest.raises(XTokenStoreError, match="invalid result"):
        reseed_x_token_state(validated_new_state(), store, expected_revision="1")


def test_reseed_failures_and_representations_expose_no_tokens() -> None:
    store = FakeStore()
    store.mode = "lost"

    with pytest.raises(XTokenStoreError) as captured:
        reseed_x_token_state(validated_new_state(), store, expected_revision="1")

    rendered = " ".join((repr(validated_new_state()), repr(captured.value), str(captured.value)))
    for secret in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH):
        assert secret not in rendered


def test_cli_failure_output_is_fixed_and_non_secret(monkeypatch, capsys) -> None:
    secret = "credential-that-must-not-escape"

    class FailingReader:
        def __init__(self, **kwargs) -> None:
            pass

        def read(self, path, *, expected_user_id):
            raise RuntimeError(secret)

    monkeypatch.setattr("fpl_bot.x_token_reseed_cli.LocalDpapiTokenStateReader", FailingReader)

    exit_code = main(
        [
            "--project-id=fpl-bot-test",
            "--project-number=123456789012",
            "--secret-id=token-state",
            f"--expected-user-id={USER_ID}",
            "--expected-revision=1",
            "--token-file=C:\\external\\token.dpapi",
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert json.loads(output.err) == {"result": "reseed_failed"}
    assert secret not in output.err


def test_reseed_dependency_graph_has_no_refresh_identity_or_post_capability() -> None:
    import fpl_bot.x_token_reseed as module
    import fpl_bot.x_token_reseed_cli as cli

    source = inspect.getsource(module) + inspect.getsource(cli)
    forbidden = (
        "XApiClient",
        "XIdentityClient",
        "PostExecutionCoordinator",
        "DeadlineExecutionRevalidator",
        "RefreshingXAccessTokenProvider",
        "XOAuthRefreshClient",
        "create_text_post",
        "/2/tweets",
        "/2/users/me",
    )
    assert all(name not in source for name in forbidden)


def test_secure_capture_and_authorization_scripts_preserve_secret_boundaries() -> None:
    capture = Path("deploy/capture-x-oauth-client-secret.ps1").read_text(encoding="utf-8")
    authorize = Path("deploy/authorize-x-test-account.ps1").read_text(encoding="utf-8")

    assert "Read-Host" in capture and "-AsSecureString" in capture
    assert "ConvertFrom-SecureString" in capture
    assert "ConvertTo-SecureString" in capture
    assert "File]::Replace" in capture
    assert ".backup-" in capture
    assert "ClientSecret" not in capture.split("param(", 1)[1].split(")", 1)[0]
    assert "ConvertTo-SecureString" in authorize
    assert "$env:X_OAUTH_CLIENT_SECRET" in authorize
    assert "Remove-Item Env:X_OAUTH_CLIENT_SECRET" in authorize
    assert "$env:X_EXPECTED_USER_ID" in authorize
    assert "fpl-bot-x-authorize" in authorize
    assert "POST /2/tweets" not in capture + authorize
