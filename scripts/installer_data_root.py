"""Create and roll back the exact private plugin data root."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_security import require_directory
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import FileIdentity, file_identity
from scripts.private_root import PrivateRootError, ensure_private_root
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

_DATA_NAME: Final = "codex-must-work-codex-must-work-local"
_MARKER: Final = ".private-root-v1"
_MARKER_BYTES: Final = b"private-root-v1\n"
_INVALID: Final = "plugin_data_root_invalid"
_CLEANUP_CONFLICT: Final = "plugin_data_cleanup_conflict"


@dataclass(frozen=True, slots=True)
class DataRootPublication:
    """Bind a newly created data root to its original filesystem identity."""

    path: Path
    created_by_run: bool
    identity: FileIdentity


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


def remove_created_data_root(publication: DataRootPublication) -> None:
    """Remove only the unchanged private root created by this transaction."""
    if not publication.created_by_run:
        return
    root = publication.path
    marker = root / _MARKER
    try:
        if file_identity(root.lstat()) != publication.identity:
            _fail(_CLEANUP_CONFLICT)
        ensure_private_root(root)
        names = tuple(root.iterdir())
        if names != (marker,):
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
