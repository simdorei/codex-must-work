"""Enumerate and snapshot direct CODEX_HOME runtimes."""

from __future__ import annotations

import hashlib
import os
import stat
from typing import TYPE_CHECKING, Never

from scripts.codex_compatibility_types import FileSnapshot
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import file_identity
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

_RUNTIME_UNSAFE = "codex_runtime_unsafe"


def discover_runtimes(
    home: Path,
) -> tuple[tuple[FileSnapshot, ...], tuple[Path, ...], Path]:
    """Return all binary snapshots, Codex candidates, and selected candidate."""
    locations = (home / ".sandbox-bin", home / "plugins" / ".plugin-appserver")
    names = ("codex", "codex.exe")
    host_names = ("codex-code-mode-host", "codex-code-mode-host.exe")
    snapshots: dict[Path, FileSnapshot] = {}
    complete: list[list[Path]] = [[], []]
    standalone: list[Path] = []
    for index, location in enumerate(locations):
        present = {
            name: location / name for name in (*names, *host_names) if _exists(location / name)
        }
        for path in present.values():
            snapshots[path.absolute()] = _snapshot_file(path.absolute())
        for suffix in ("", ".exe"):
            codex = present.get(f"codex{suffix}")
            host = present.get(f"codex-code-mode-host{suffix}")
            if host is not None and codex is None:
                _fail("codex_runtime_incomplete")
            if codex is not None and host is not None:
                complete[index].append(codex.absolute())
            elif codex is not None and index == 0:
                standalone.append(codex.absolute())
            elif codex is not None:
                _fail("codex_runtime_incomplete")
    runtimes = tuple(sorted((path for path in snapshots if path.name in names), key=str))
    if not runtimes:
        _fail("codex_runtime_missing")
    selected_group = complete[0] or complete[1] or standalone
    if not selected_group:
        _fail("codex_runtime_incomplete")
    selected = sorted(selected_group, key=lambda path: (_native_rank(path), str(path)))[0]
    ordered = tuple(sorted(snapshots.values(), key=lambda item: str(item.path)))
    return ordered, runtimes, selected


def _snapshot_file(path: Path) -> FileSnapshot:
    try:
        named = path.lstat()
        if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1 or _is_reparse(named):
            _fail(_RUNTIME_UNSAFE)
        descriptor = open_direct_file(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            while chunk := handle.read(1_048_576):
                digest.update(chunk)
            opened = os.fstat(handle.fileno())
    except OSError as error:
        raise InstallPluginError(_RUNTIME_UNSAFE) from error
    if file_identity(named) != file_identity(opened):
        _fail(_RUNTIME_UNSAFE)
    return FileSnapshot(path, file_identity(opened), digest.hexdigest())


def _exists(path: Path) -> bool:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _fail(_RUNTIME_UNSAFE)
    return True


def _native_rank(path: Path) -> int:
    return int((os.name == "nt") != path.name.endswith(".exe"))


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(metadata, "st_file_attributes", 0) & flag)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
