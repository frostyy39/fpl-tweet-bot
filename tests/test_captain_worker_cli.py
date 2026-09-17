import getpass
import json

from fpl_bot import captain_worker_cli as cli


def test_no_assignment_cli_writes_private_utf8_audit_without_touching_browser(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "controller_origin": "https://captain-controller.example",
                "profile_dir": str(tmp_path / "unopened-profile"),
                "expected_user": getpass.getuser(),
                "audit_parent": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def __init__(self, *_):
            pass

        def obtain_assignment(self, run_id):
            return None

    monkeypatch.setattr(cli, "CaptainWorkerHttpClient", Client)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    monkeypatch.setattr(
        cli.ProvenBrowserAcquisition,
        "acquire",
        lambda *_: (_ for _ in ()).throw(AssertionError("no browser")),
    )
    assert cli.main(["--config", str(config)]) == 0
    audits = list(tmp_path.glob("*/audit.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_bytes().decode("utf-8", errors="strict"))
    assert audit["status"] == "no_assignment" and audit["no_post"] is True
    assert audit["handoff"] is None
    assert not (tmp_path / "unopened-profile").exists()


def test_invalid_cli_configuration_fails_closed_before_metadata_or_browser(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config.json"
    config.write_text('{"password":"never-used"}', encoding="utf-8")
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    assert cli.main(["--config", str(config)]) == 2
    assert capsys.readouterr().out == "captain_worker_failed_closed\n"
    assert list(tmp_path.glob("*/audit.json")) == []
