"""Validate portable-runtime archive membership before extraction."""

from __future__ import annotations

import hashlib
import posixpath
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Never

from scripts.install_errors import InstallPluginError

_INVALID: Final = "portable_runtime_invalid"
_MINIMUM_ARCHIVE_PARTS: Final = 2


@dataclass(frozen=True, slots=True)
class ArchiveExclusion:
    """One byte-for-byte pinned archive member omitted from installation."""

    path: str
    size: int
    sha256: str
    executable: bool


ArchiveMember = tuple[PurePosixPath, tarfile.TarInfo]


def validated_archive_members(
    bundle: tarfile.TarFile,
    exclusions: tuple[ArchiveExclusion, ...],
) -> tuple[ArchiveMember, ...]:
    """Validate all members, then remove only the exact pinned bytecode set."""
    members = _structurally_valid_members(bundle)
    expected = {entry.path: entry for entry in exclusions}
    included: list[ArchiveMember] = []
    observed: set[str] = set()
    for relative, member in members:
        if relative.suffix != ".pyc":
            included.append((relative, member))
            continue
        exclusion = expected.get(relative.as_posix())
        if exclusion is None or not member.isfile():
            _fail()
        extracted = bundle.extractfile(member)
        if extracted is None:
            _fail()
        data = extracted.read()
        if (
            len(data) != exclusion.size
            or hashlib.sha256(data).hexdigest() != exclusion.sha256
            or bool(member.mode & 0o111) != exclusion.executable
        ):
            _fail()
        observed.add(exclusion.path)
    if observed != set(expected):
        _fail()
    return tuple(included)


def resolved_member(
    member: tarfile.TarInfo,
    members: tuple[ArchiveMember, ...],
) -> tarfile.TarInfo:
    """Resolve an allowed archive link to one validated regular file."""
    if member.isfile():
        return member
    member_map = {item.name: item for _, item in members}
    target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
    resolved = member_map.get(target)
    if resolved is None or not resolved.isfile():
        _fail()
    return resolved


def _structurally_valid_members(bundle: tarfile.TarFile) -> tuple[ArchiveMember, ...]:
    members: list[ArchiveMember] = []
    paths: set[str] = set()
    for member in bundle.getmembers():
        path = PurePosixPath(member.name)
        if (
            not _normalized(member.name)
            or len(path.parts) < _MINIMUM_ARCHIVE_PARTS
            or path.parts[0] != "python"
            or member.name in paths
            or not (member.isfile() or member.issym() or member.islnk())
        ):
            _fail()
        paths.add(member.name)
        members.append((PurePosixPath(*path.parts[1:]), member))
    relative_paths = {relative.as_posix() for relative, _ in members}
    if len(relative_paths) != len(members):
        _fail()
    for relative in relative_paths:
        parts = relative.split("/")
        if any("/".join(parts[:index]) in relative_paths for index in range(1, len(parts))):
            _fail()
    return tuple(members)


def _normalized(path: str) -> bool:
    pure = PurePosixPath(path)
    return (
        bool(path)
        and "\\" not in path
        and not pure.is_absolute()
        and ".." not in pure.parts
        and path == pure.as_posix()
    )


def _fail() -> Never:
    raise InstallPluginError(_INVALID)
