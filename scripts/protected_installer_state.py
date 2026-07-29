"""Read and atomically publish HMAC-authenticated installer state records."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final, Never, Protocol

from scripts.cache_types import identity
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import InstallerLease, require_live_lease
from scripts.private_root import PrivateRootError, ensure_private_root, verify_private_root
from scripts.state_io import open_direct_file
from scripts.windows_file import flush_directory

type JsonValue = str | int | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_ROOT_NAME: Final = ".cmw-installer-state"
_KEY_NAME: Final = "install-receipt-v1.key"
_KEY_BYTES: Final = 32
_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR
_INVALID: Final = "uninstall_receipt_invalid"
_MISSING: Final = "uninstall_receipt_reinstall_required"
_WRITE_FAILED: Final = "install_receipt_publication_failed"


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


def write_signed_record(lease: InstallerLease, name: str, payload: JsonObject) -> None:
    """Sign and atomically replace one record inside the protected root."""
    require_live_lease(lease)
    root = _root(lease)
    key = _load_or_create_key(root / _KEY_NAME)
    if len(key) != _KEY_BYTES:
        _fail(_INVALID)
    envelope: JsonObject = {
        "payload": payload,
        "hmac_sha256": hmac.new(key, canonical(payload), hashlib.sha256).hexdigest(),
    }
    _atomic_write(root, root / name, canonical(envelope))


def read_signed_record(
    lease: InstallerLease,
    name: str,
    *,
    missing_reason: str = _MISSING,
) -> JsonObject:
    """Authenticate one exact protected record."""
    require_live_lease(lease)
    root = _root(lease, create=False)
    key = _read_exact_file(root / _KEY_NAME, missing=missing_reason)
    if len(key) != _KEY_BYTES:
        _fail(_INVALID)
    envelope = _parse_object(_read_exact_file(root / name, missing=missing_reason))
    payload = envelope.get("payload")
    signature = envelope.get("hmac_sha256")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        _fail(_INVALID)
    expected = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        _fail(_INVALID)
    return payload


def remove_record(lease: InstallerLease, name: str) -> None:
    """Remove only one direct regular protected record."""
    require_live_lease(lease)
    root = _root(lease, create=False)
    path = root / name
    try:
        metadata = path.lstat()
        _require_regular(metadata)
        path.unlink()
        flush_directory(root)
    except FileNotFoundError:
        return
    except (OSError, PrivateRootError) as error:
        raise InstallPluginError(_INVALID) from error


def canonical(value: JsonValue) -> bytes:
    """Encode one JSON value for stable HMAC input."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _root(lease: InstallerLease, *, create: bool = True) -> Path:
    root = lease.home / _ROOT_NAME
    try:
        if create:
            ensure_private_root(root)
        else:
            verify_private_root(root)
    except (OSError, PrivateRootError) as error:
        reason = _INVALID if root.exists() else _MISSING
        raise InstallPluginError(reason) from error
    return root


def _load_or_create_key(path: Path) -> bytes:
    try:
        return _read_exact_file(path, missing=_WRITE_FAILED)
    except InstallPluginError as error:
        if error.reason_code != _WRITE_FAILED:
            raise
    key = secrets.token_bytes(_KEY_BYTES)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        _FILE_MODE,
    )
    with os.fdopen(descriptor, "wb") as handle:
        _ = handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(_FILE_MODE)
    flush_directory(path.parent)
    return key


def _atomic_write(root: Path, target: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".receipt.", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _ = handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(_FILE_MODE)
        _ = temporary.replace(target)
        flush_directory(root)
    except OSError as error:
        raise InstallPluginError(_WRITE_FAILED) from error
    finally:
        with suppress(FileNotFoundError):
            _ = temporary.unlink()


def _read_exact_file(path: Path, *, missing: str) -> bytes:
    try:
        named = path.lstat()
        _require_regular(named)
        descriptor = open_direct_file(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read()
            opened = os.fstat(handle.fileno())
        _require_regular(opened)
        if identity(named) != identity(opened):
            _fail(_INVALID)
    except FileNotFoundError as error:
        raise InstallPluginError(missing) from error
    except OSError as error:
        raise InstallPluginError(_INVALID) from error
    else:
        return data


def _require_regular(metadata: os.stat_result) -> None:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
    ):
        _fail(_INVALID)


def _parse_object(data: bytes) -> JsonObject:
    try:
        value = _LOAD_JSON(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InstallPluginError(_INVALID) from error
    if not isinstance(value, dict):
        _fail(_INVALID)
    return value


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
