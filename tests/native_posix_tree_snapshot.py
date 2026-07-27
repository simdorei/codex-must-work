"""Metadata-aware filesystem snapshots for native POSIX smoke checks."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from pathlib import Path

_NO_METADATA_ONLY: Final[frozenset[Path]] = frozenset()


@dataclass(frozen=True, slots=True)
class TreeEntry:
    relative: str
    kind: Literal["directory", "file", "symlink"]
    mode: int
    device: int
    inode: int
    size: int
    links: int
    modified_ns: int
    digest: str | None


def tree_snapshot(
    root: Path,
    metadata_only: frozenset[Path] = _NO_METADATA_ONLY,
) -> tuple[TreeEntry, ...]:
    """Snapshot exact tree state without opening explicitly secret files."""
    entries: list[TreeEntry] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: item.as_posix().encode()):
        metadata = path.lstat()
        kind: Literal["directory", "file", "symlink"]
        digest: str | None = None
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            if path not in metadata_only:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            kind = "symlink"
        entries.append(
            TreeEntry(
                "." if path == root else path.relative_to(root).as_posix(),
                kind,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_nlink,
                metadata.st_mtime_ns,
                digest,
            )
        )
    return tuple(entries)
