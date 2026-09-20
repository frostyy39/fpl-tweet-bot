from datetime import UTC, datetime
from pathlib import Path

from fpl_bot.x_oauth import OAUTH_SCOPES
from fpl_bot.x_production_authorization_cli import main
from fpl_bot.x_reauthorization_cli import ReauthorizationMetadata


def test_production_authorization_discovers_identity_without_posting_or_expected_id(
    tmp_path: Path, monkeypatch, capsys
):
    observed = {}

    def authorize(**kwargs):
        observed.update(kwargs)
        return ReauthorizationMetadata(
            user_id="987654321012345678",
            scopes=OAUTH_SCOPES,
            refresh_token_present=True,
            token_type="bearer",
            expires_at_utc=datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            handoff_saved=True,
        )

    monkeypatch.setattr(
        "fpl_bot.x_production_authorization_cli.authorize_from_local_credentials",
        authorize,
    )
    exit_code = main(
        [
            f"--client-id-path={tmp_path / 'id'}",
            f"--encrypted-client-secret-path={tmp_path / 'secret'}",
            f"--token-output-path={tmp_path / 'tokens.dpapi'}",
            f"--repository-root={tmp_path}",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert observed["expected_user_id"] is None
    assert "verified_user_id=987654321012345678" in captured.out
    assert "required_scopes_present=true" in captured.out
    assert "synthetic" not in captured.out
    assert captured.err == ""


def test_production_authorization_failure_is_redacted(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        "fpl_bot.x_production_authorization_cli.authorize_from_local_credentials",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("secret-value")),
    )
    exit_code = main(
        [
            f"--client-id-path={tmp_path / 'id'}",
            f"--encrypted-client-secret-path={tmp_path / 'secret'}",
            f"--token-output-path={tmp_path / 'tokens.dpapi'}",
            f"--repository-root={tmp_path}",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "authorization_succeeded=false\n"
    assert "secret-value" not in captured.err
