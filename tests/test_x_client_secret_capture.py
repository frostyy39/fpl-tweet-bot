from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_bot.x_client_secret_capture import (
    CLIENT_SECRET_DPAPI_MAGIC,
    XClientSecretCaptureError,
    capture_client_secret,
    load_client_secret,
)


class ReversibleProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("invalid synthetic payload")
        return ciphertext.removeprefix(b"protected:")[::-1]


def test_synthetic_capture_round_trip_replaces_atomically_and_keeps_backup(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "x_client_secret.encrypted"
    target.write_bytes(b"prior-encrypted-state")

    result = capture_client_secret(
        target,
        repository_root=repository,
        secret_reader=lambda _prompt: "synthetic-client-secret-for-test-only",
        protector=ReversibleProtector(),
        now=lambda: datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )

    assert result.round_trip_validated is True
    assert result.backup_created is True
    assert result.atomic_replacement is True
    assert target.read_bytes().startswith(CLIENT_SECRET_DPAPI_MAGIC)
    assert load_client_secret(target, protector=ReversibleProtector()) == (
        "synthetic-client-secret-for-test-only"
    )
    assert (external / "x_client_secret.encrypted.backup-20260903T120000Z").read_bytes() == (
        b"prior-encrypted-state"
    )
    assert not list(external.glob(".*.pending-*"))


def test_capture_failure_preserves_existing_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "x_client_secret.encrypted"
    target.write_bytes(b"prior-encrypted-state")

    with pytest.raises(XClientSecretCaptureError):
        capture_client_secret(
            target,
            repository_root=repository,
            secret_reader=lambda _prompt: "",
            protector=ReversibleProtector(),
        )

    assert target.read_bytes() == b"prior-encrypted-state"
    assert not list(external.glob("*.backup-*"))
    assert not list(external.glob(".*.pending-*"))


def test_capture_rejects_repository_destination_before_prompt(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    prompted = False

    def reader(_prompt: str) -> str:
        nonlocal prompted
        prompted = True
        return "synthetic"

    with pytest.raises(XClientSecretCaptureError):
        capture_client_secret(
            repository / "secret.encrypted",
            repository_root=repository,
            secret_reader=reader,
            protector=ReversibleProtector(),
        )

    assert prompted is False


def test_capture_error_and_result_representations_do_not_contain_secret(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    secret = "synthetic-client-secret-never-report"

    result = capture_client_secret(
        external / "secret.encrypted",
        repository_root=repository,
        secret_reader=lambda _prompt: secret,
        protector=ReversibleProtector(),
    )

    assert secret not in repr(result)
    with pytest.raises(XClientSecretCaptureError) as error:
        capture_client_secret(
            external / "second.encrypted",
            repository_root=repository,
            secret_reader=lambda _prompt: f" {secret}",
            protector=ReversibleProtector(),
        )
    assert secret not in str(error.value)
