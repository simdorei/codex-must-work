"""Provision and verify restart-stable per-session control capabilities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
from string import ascii_letters, digits
from typing import TYPE_CHECKING, Final, final, override

from scripts.private_root import ensure_private_root
from scripts.state_io import UnsafeStatePathError, open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

_KEY_NAME: Final = "control.key"
_KEY_BYTES: Final = 32
_CAPABILITY_CHARS: Final = 43
_MAX_SESSION_BYTES: Final = 65_536
_FILE_MODE: Final = stat.S_IRUSR | stat.S_IWUSR
_DOMAIN_PREFIX: Final = b"cmw-control-v1\\0"
_DOMAIN_SUFFIX: Final = b"\\0codex-must-work@codex-must-work-local"
_BASE64URL_CHARS: Final = frozenset(ascii_letters + digits + "-_")
_RECOVERY_REQUIRED: Final = "control_key_recovery_required"
_KEY_INVALID: Final = "control_key_invalid"
_KEY_UNAVAILABLE: Final = "control_key_unavailable"
_SESSION_INVALID: Final = "control_session_invalid"
_KEY_CHANGED: Final = "control_key_changed"


@final
class ControlKeyError(RuntimeError):
    """Expose one public-safe control-key recovery reason."""

    def __init__(self, reason_code: str) -> None:
        """Retain one stable reason without storing key material."""
        super().__init__(reason_code)
        self.reason_code = reason_code

    @override
    def __str__(self) -> str:
        return self.reason_code


@final
class _InvalidKeyError(Exception):
    def __init__(
        self,
        *,
        replaceable: bool,
        device: int | None = None,
        inode: int | None = None,
    ) -> None:
        super().__init__("invalid control key")
        self.replaceable = replaceable
        self.device = device
        self.inode = inode


def provision_control_key(plugin_data: Path, active_state_root: Path) -> bytes:
    """Create a master key only when no active task can depend on an older key."""
    ensure_private_root(plugin_data)
    key_path = plugin_data / _KEY_NAME
    try:
        return _read_key(key_path)
    except FileNotFoundError:
        invalid: _InvalidKeyError | None = None
    except _InvalidKeyError as error:
        invalid = error
    if _active_state_exists(active_state_root):
        raise ControlKeyError(_RECOVERY_REQUIRED) from None
    if invalid is not None:
        if not invalid.replaceable or invalid.device is None or invalid.inode is None:
            raise ControlKeyError(_KEY_INVALID) from None
        _unlink_unchanged(key_path, invalid.device, invalid.inode)
    try:
        return _create_key(key_path)
    except FileExistsError:
        try:
            return _read_key(key_path)
        except (FileNotFoundError, _InvalidKeyError) as error:
            raise ControlKeyError(_KEY_INVALID) from error


def load_control_key(plugin_data: Path, active_state_root: Path | None = None) -> bytes:
    """Load an installed master key without creating or rotating it."""
    ensure_private_root(plugin_data)
    try:
        return _read_key(plugin_data / _KEY_NAME)
    except (FileNotFoundError, _InvalidKeyError) as error:
        reason = (
            _RECOVERY_REQUIRED
            if active_state_root is not None and _active_state_exists(active_state_root)
            else _KEY_UNAVAILABLE
        )
        raise ControlKeyError(reason) from error


def derive_control_capability(key: bytes, session_id: str) -> str:
    """Derive one unversioned base64url bearer capability for a session."""
    if len(key) != _KEY_BYTES:
        raise ControlKeyError(_KEY_INVALID)
    encoded_session = session_id.encode("utf-8")
    if not encoded_session or len(encoded_session) > _MAX_SESSION_BYTES:
        raise ControlKeyError(_SESSION_INVALID)
    digest = hmac.new(
        key,
        _DOMAIN_PREFIX + encoded_session + _DOMAIN_SUFFIX,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_control_capability(key: bytes, session_id: str, capability: str) -> bool:
    """Compare a well-shaped capability in constant time."""
    try:
        encoded_session = session_id.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if (
        len(key) != _KEY_BYTES
        or not encoded_session
        or len(encoded_session) > _MAX_SESSION_BYTES
        or len(capability) != _CAPABILITY_CHARS
        or any(character not in _BASE64URL_CHARS for character in capability)
    ):
        return False
    expected = derive_control_capability(key, session_id)
    return hmac.compare_digest(expected, capability)


def _read_key(path: Path) -> bytes:
    try:
        named = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise _InvalidKeyError(replaceable=False) from error
    if not _metadata_is_direct(named):
        raise _InvalidKeyError(replaceable=False)
    if os.name != "nt" and stat.S_IMODE(named.st_mode) != _FILE_MODE:
        raise _InvalidKeyError(
            replaceable=True,
            device=named.st_dev,
            inode=named.st_ino,
        )
    try:
        descriptor = open_direct_file(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except UnsafeStatePathError as error:
        raise _InvalidKeyError(replaceable=False) from error
    with os.fdopen(descriptor, "rb") as handle:
        key = handle.read(_KEY_BYTES + 1)
        opened = os.fstat(handle.fileno())
    if not _metadata_is_direct(opened):
        raise _InvalidKeyError(replaceable=False)
    if len(key) != _KEY_BYTES:
        raise _InvalidKeyError(
            replaceable=True,
            device=named.st_dev,
            inode=named.st_ino,
        )
    return key


def _metadata_is_direct(metadata: os.stat_result) -> bool:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return False
    if os.name == "nt":
        return not bool(metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return True


def _create_key(path: Path) -> bytes:
    key = secrets.token_bytes(_KEY_BYTES)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, _FILE_MODE)
    created = os.fstat(descriptor)
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _ = handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(_FILE_MODE)
        if _read_key(path) != key:
            raise ControlKeyError(_KEY_INVALID)
        complete = True
        return key
    finally:
        if not complete:
            _unlink_unchanged(path, created.st_dev, created.st_ino)


def _unlink_unchanged(path: Path, device: int, inode: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if metadata.st_dev != device or metadata.st_ino != inode:
        raise ControlKeyError(_KEY_CHANGED)
    path.unlink()


def _active_state_exists(root: Path) -> bool:
    runtime = root / "runtime"
    try:
        metadata = runtime.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (os.name == "nt" and metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    ):
        return True
    try:
        return any(path.name.endswith(".json") for path in runtime.iterdir())
    except OSError:
        return True
