from datetime import UTC, datetime
from pathlib import Path

from fpl_bot.x_api import AuthenticatedXUser
from fpl_bot.x_client_secret_capture import CLIENT_SECRET_DPAPI_MAGIC
from fpl_bot.x_oauth import OAUTH_SCOPES, OAuthTokenBundle
from fpl_bot.x_reauthorization_cli import authorize_from_local_credentials, main

USER_ID = "123456789"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class ReversibleProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        return ciphertext.removeprefix(b"protected:")[::-1]


def test_reauthorization_uses_local_credentials_and_saves_only_after_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    client_id = external / "x_client_id.txt"
    client_id.write_text("synthetic-client-id", encoding="utf-8")
    secret = "synthetic-client-secret"
    encrypted = external / "x_client_secret.encrypted"
    encrypted.write_bytes(CLIENT_SECRET_DPAPI_MAGIC + b"protected:" + secret.encode()[::-1])
    output = external / "fresh.dpapi"
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        "fpl_bot.x_reauthorization_cli.load_client_secret",
        lambda path: (
            ReversibleProtector()
            .unprotect(path.read_bytes()[len(CLIENT_SECRET_DPAPI_MAGIC) :])
            .decode()
        ),
    )
    monkeypatch.setattr(
        "fpl_bot.x_oauth.WindowsDpapiProtector",
        ReversibleProtector,
    )

    def authorizer(config, *, receiver, handoff, browser_open):
        observed["client_id"] = config.credentials.client_id
        observed["client_secret"] = config.credentials.client_secret
        observed["expected_user_id"] = config.expected_user_id
        tokens = OAuthTokenBundle(
            access_token="synthetic-access",
            refresh_token="synthetic-refresh",
            token_type="bearer",
            scopes=OAUTH_SCOPES,
            expires_in_seconds=7200,
            received_at_utc=NOW,
        )
        user = AuthenticatedXUser(user_id=USER_ID, username="FPLBotTest")
        handoff.store(tokens, user)
        return user

    metadata = authorize_from_local_credentials(
        client_id_path=client_id,
        encrypted_client_secret_path=encrypted,
        expected_user_id=USER_ID,
        token_output_path=output,
        repository_root=repository,
        authorizer=authorizer,
        browser_open=lambda _url: True,
    )

    assert observed == {
        "client_id": "synthetic-client-id",
        "client_secret": secret,
        "expected_user_id": USER_ID,
    }
    assert metadata.user_id == USER_ID
    assert metadata.scopes == OAUTH_SCOPES
    assert metadata.refresh_token_present is True
    assert metadata.token_type == "bearer"
    assert metadata.expires_at_utc == "2026-09-03T14:00:00Z"
    assert metadata.handoff_saved is True
    assert output.exists()
    assert secret not in repr(metadata)


def test_reauthorization_cli_reports_encrypted_secret_failure_without_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    client_id = tmp_path / "x_client_id.txt"
    client_id.write_text("synthetic-client-id", encoding="utf-8")
    encrypted = tmp_path / "x_client_secret.encrypted"
    encrypted.write_bytes(b"unsupported-local-format")
    output = tmp_path / "fresh.dpapi"

    exit_code = main(
        [
            f"--client-id-path={client_id}",
            f"--encrypted-client-secret-path={encrypted}",
            f"--expected-user-id={USER_ID}",
            f"--token-output-path={output}",
            f"--repository-root={tmp_path}",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "authorization_succeeded=false\n"
    assert "Traceback" not in captured.err
