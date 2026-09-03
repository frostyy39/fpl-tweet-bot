"""Safely capture a local OAuth Client Secret with current-user Windows DPAPI."""

import argparse
import ctypes
import getpass
import os
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fpl_bot.errors import FplBotError
from fpl_bot.windows_dpapi import DpapiProtectionError, WindowsDpapiProtector

CLIENT_SECRET_DPAPI_MAGIC = b"FPLBOT-X-CLIENT-SECRET-DPAPI-V1\x00"
REPLACEFILE_WRITE_THROUGH = 0x00000001


class XClientSecretCaptureError(FplBotError):
    """Raised without credential material when secure local capture fails."""


@dataclass(frozen=True, slots=True)
class XClientSecretCaptureResult:
    """Non-secret outcome of one completed local capture."""

    round_trip_validated: bool
    backup_created: bool
    atomic_replacement: bool


def capture_client_secret(
    output_path: Path,
    *,
    repository_root: Path,
    secret_reader: Callable[[str], str] = getpass.getpass,
    protector: WindowsDpapiProtector | None = None,
    now: Callable[[], datetime] | None = None,
) -> XClientSecretCaptureResult:
    """Capture one hidden value, validate its DPAPI round trip, then replace atomically."""

    target = _validated_external_target(output_path, repository_root)
    secret_value = secret_reader("Paste the newly generated OAuth 2.0 Client Secret: ")
    secret_bytes = bytearray()
    recovered = bytearray()
    pending = target.with_name(f".{target.name}.pending-{secrets.token_hex(16)}")
    active_protector = protector or WindowsDpapiProtector()
    try:
        _validate_secret(secret_value)
        secret_bytes.extend(secret_value.encode("utf-8"))
        protected = active_protector.protect(bytes(secret_bytes))
        if not isinstance(protected, bytes) or not protected:
            raise XClientSecretCaptureError("Current-user DPAPI returned no encrypted credential")
        if bytes(secret_bytes) in protected:
            raise XClientSecretCaptureError(
                "Encrypted credential failed plaintext safety validation"
            )
        recovered.extend(active_protector.unprotect(protected))
        if not secrets.compare_digest(secret_bytes, recovered):
            raise XClientSecretCaptureError(
                "Encrypted credential failed DPAPI round-trip validation"
            )

        _write_exclusive(pending, CLIENT_SECRET_DPAPI_MAGIC + protected)
        if target.exists():
            timestamp = (now or (lambda: datetime.now(UTC)))().astimezone(UTC)
            backup = target.with_name(
                f"{target.name}.backup-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
            )
            if backup.exists():
                raise XClientSecretCaptureError(
                    "A timestamped backup already exists; the prior encrypted file was unchanged"
                )
            _replace_file_with_backup(target, pending, backup)
            return XClientSecretCaptureResult(True, True, True)

        os.replace(pending, target)
        return XClientSecretCaptureResult(True, False, True)
    except (DpapiProtectionError, OSError, UnicodeError) as exc:
        raise XClientSecretCaptureError("Secure Client Secret capture failed") from exc
    finally:
        secret_value = ""
        _zero(secret_bytes)
        _zero(recovered)
        pending.unlink(missing_ok=True)


def load_client_secret(
    encrypted_path: Path,
    *,
    protector: WindowsDpapiProtector | None = None,
) -> str:
    """Load a Client Secret captured by this module without exposing it in representations."""

    try:
        payload = encrypted_path.read_bytes()
    except OSError as exc:
        raise XClientSecretCaptureError("Encrypted Client Secret could not be read") from exc
    if not payload.startswith(CLIENT_SECRET_DPAPI_MAGIC):
        raise XClientSecretCaptureError("Encrypted Client Secret has an unsupported format")
    protected = payload[len(CLIENT_SECRET_DPAPI_MAGIC) :]
    try:
        plaintext = bytearray((protector or WindowsDpapiProtector()).unprotect(protected))
        value = plaintext.decode("utf-8")
        _validate_secret(value)
        return value
    except (DpapiProtectionError, UnicodeError) as exc:
        raise XClientSecretCaptureError("Encrypted Client Secret could not be decrypted") from exc
    finally:
        if "plaintext" in locals():
            _zero(plaintext)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture an OAuth Client Secret with DPAPI")
    parser.add_argument("--output-path", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = capture_client_secret(
            arguments.output_path,
            repository_root=arguments.repository_root,
        )
    except XClientSecretCaptureError:
        print(
            "Secure Client Secret capture failed; the prior encrypted file was preserved.",
            file=sys.stderr,
        )
        return 1
    print("secure_capture_succeeded=true")
    print(f"dpapi_round_trip_succeeded={str(result.round_trip_validated).lower()}")
    print(f"previous_secret_backup_created={str(result.backup_created).lower()}")
    print(f"atomic_replacement_succeeded={str(result.atomic_replacement).lower()}")
    return 0


def _validated_external_target(output_path: Path, repository_root: Path) -> Path:
    if not output_path.is_absolute():
        raise XClientSecretCaptureError("Encrypted Client Secret path must be absolute")
    if output_path.is_symlink():
        raise XClientSecretCaptureError("Encrypted Client Secret path must not be a symbolic link")
    try:
        target = output_path.resolve(strict=False)
        root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise XClientSecretCaptureError("Client Secret path validation failed") from exc
    if target == root or root in target.parents:
        raise XClientSecretCaptureError(
            "Encrypted Client Secret must remain outside the repository"
        )
    if not target.parent.is_dir():
        raise XClientSecretCaptureError("Encrypted Client Secret parent directory does not exist")
    return target


def _validate_secret(value: str) -> None:
    if not value or value != value.strip() or not value.isprintable():
        raise XClientSecretCaptureError("Client Secret input was empty or invalid")


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_file_with_backup(target: Path, replacement: Path, backup: Path) -> None:
    if os.name != "nt":
        raise XClientSecretCaptureError("Atomic replacement with backup requires Windows")
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        raise XClientSecretCaptureError("Windows atomic file replacement is unavailable") from None
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    replace_file.restype = ctypes.c_int
    if not replace_file(
        str(target),
        str(replacement),
        str(backup),
        REPLACEFILE_WRITE_THROUGH,
        None,
        None,
    ):
        raise XClientSecretCaptureError("Encrypted Client Secret atomic replacement failed")


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


if __name__ == "__main__":
    raise SystemExit(main())
