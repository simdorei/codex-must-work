"""Create and roll back the exact private plugin data root."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_security import require_directory
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import FileIdentity, file_identity
from scripts.marketplace_identity import DATA_ROOT_NAME
from scripts.private_root import PrivateRootError, ensure_private_root
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

_DATA_NAME: Final = DATA_ROOT_NAME
_MARKER: Final = ".private-root-v1"
_MARKER_BYTES: Final = b"private-root-v1\n"
_INVALID: Final = "plugin_data_root_invalid"
_CLEANUP_CONFLICT: Final = "plugin_data_cleanup_conflict"
_CONTROL_KEY_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class DataRootPublication:
    """Bind a newly created data root to its original filesystem identity."""

    path: Path
    created_by_run: bool
    identity: FileIdentity
    control_key_identity: FileIdentity | None = None
    control_key_digest: bytes | None = None


def prepare_data_root(codex_home: Path) -> DataRootPublication:
    """Create or verify Codex's exact private data root after preflight."""
    plugins = _ordinary_directory(codex_home / "plugins")
    data = _ordinary_directory(plugins / "data")
    root = data / _DATA_NAME
    try:
        _ = root.lstat()
    except FileNotFoundError:
        existed = False
    except OSError as error:
        raise InstallPluginError(_INVALID) from error
    else:
        existed = True
    try:
        ensure_private_root(root)
        identity = file_identity(root.lstat())
    except (OSError, PrivateRootError) as error:
        raise InstallPluginError(_INVALID) from error
    return DataRootPublication(root, created_by_run=not existed, identity=identity)


def bind_created_control_key(
    publication: DataRootPublication,
    key: bytes,
) -> DataRootPublication:
    """Bind rollback to the exact control key created inside a new data root."""
    if not publication.created_by_run:
        return publication
    path = publication.path / "control.key"
    return DataRootPublication(
        path=publication.path,
        created_by_run=True,
        identity=publication.identity,
        control_key_identity=file_identity(path.lstat()),
        control_key_digest=hashlib.sha256(key).digest(),
    )


def remove_created_data_root(publication: DataRootPublication) -> None:
    """Remove only the unchanged private root created by this transaction."""
    if not publication.created_by_run:
        return
    root = publication.path
    marker = root / _MARKER
    control_key = root / "control.key"
    try:
        if file_identity(root.lstat()) != publication.identity:
            _fail(_CLEANUP_CONFLICT)
        ensure_private_root(root)
        names = frozenset(root.iterdir())
        expected = (
            frozenset((marker, control_key))
            if publication.control_key_identity is not None
            else frozenset((marker,))
        )
        if names != expected:
            _fail(_CLEANUP_CONFLICT)
        if publication.control_key_identity is not None:
            descriptor = open_direct_file(
                control_key,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            with os.fdopen(descriptor, "rb") as handle:
                key = handle.read(33)
                opened = os.fstat(handle.fileno())
            digest = publication.control_key_digest
            if (
                file_identity(opened) != publication.control_key_identity
                or len(key) != _CONTROL_KEY_BYTES
                or digest is None
                or not hmac.compare_digest(hashlib.sha256(key).digest(), digest)
            ):
                _fail(_CLEANUP_CONFLICT)
        named = marker.lstat()
        descriptor = open_direct_file(marker, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as handle:
            contents = handle.read()
            opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or file_identity(opened) != file_identity(named)
            or contents != _MARKER_BYTES
        ):
            _fail(_CLEANUP_CONFLICT)
        if publication.control_key_identity is not None:
            control_key.unlink()
        marker.unlink()
        root.rmdir()
    except InstallPluginError:
        raise
    except (OSError, PrivateRootError) as error:
        raise InstallPluginError(_CLEANUP_CONFLICT) from error


def _ordinary_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        raise InstallPluginError(_INVALID) from error
    require_directory(path, _INVALID)
    return path


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
