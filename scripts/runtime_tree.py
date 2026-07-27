"""Parse, materialize, and verify immutable portable-runtime trees."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Never, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

from scripts.cache_security import read_source, secure_path
from scripts.cache_types import CacheIdentity, identity
from scripts.install_errors import InstallPluginError
from scripts.runtime_archive import ArchiveExclusion, resolved_member, validated_archive_members
from scripts.state_io import open_direct_file

_DIRECTORY_MODE: Final = 0o700
_EXECUTABLE_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
_INVALID: Final = "portable_runtime_invalid"
_MANIFEST_INVALID: Final = "portable_runtime_manifest_invalid"
_MEMBER_KEYS: Final = frozenset({"executable", "path", "sha256", "size", "type"})
_SHA256_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class RuntimeEntry:
    """One exact object in a materialized runtime tree."""

    path: str
    size: int
    sha256: str
    type: str
    executable: bool


@dataclass(frozen=True, slots=True)
class RuntimeTreeManifest:
    """Sorted exact membership and metadata for one runtime target."""

    entries: tuple[RuntimeEntry, ...]
    excluded_bytecode: tuple[ArchiveExclusion, ...]


def load_runtime_manifest(
    path: Path,
    expected_sha256: str,
    exclusion_path: Path,
    expected_exclusion_sha256: str,
    expected_exclusion_count: int,
) -> RuntimeTreeManifest:
    """Load one hash-pinned runtime-tree manifest."""
    data = read_source(path, _MANIFEST_INVALID, open_direct_file)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        _fail(_MANIFEST_INVALID)
    try:
        raw = cast("list[dict[str, bool | int | str]]", json.loads(data))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail(_MANIFEST_INVALID)
    entries = tuple(_parse_entry(item) for item in raw)
    paths = tuple(entry.path for entry in entries)
    if not entries or paths != tuple(sorted(set(paths), key=str.encode)):
        _fail(_MANIFEST_INVALID)
    expected_directories = _directories(entry.path for entry in entries if entry.type == "file")
    actual_directories = tuple(entry.path for entry in entries if entry.type == "directory")
    if actual_directories != expected_directories:
        _fail(_MANIFEST_INVALID)
    exclusions = _load_exclusions(
        exclusion_path,
        expected_exclusion_sha256,
        expected_exclusion_count,
    )
    return RuntimeTreeManifest(entries, exclusions)


def materialize_archive(
    archive: Path,
    destination: Path,
    manifest: RuntimeTreeManifest,
) -> None:
    """Extract archive members as private regular files without preserving links."""
    root = destination / "python"
    root.mkdir(mode=_DIRECTORY_MODE)
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = validated_archive_members(bundle, manifest.excluded_bytecode)
            for relative, member in members:
                source = resolved_member(member, members)
                extracted = bundle.extractfile(source)
                if extracted is None:
                    _fail(_INVALID)
                data = extracted.read()
                target = root.joinpath(*relative.parts)
                target.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
                mode = _EXECUTABLE_MODE if source.mode & 0o111 else _FILE_MODE
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    mode,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    if os.name == "posix":
                        os.fchmod(handle.fileno(), mode)
                    _ = handle.write(data)
    except (OSError, tarfile.TarError) as error:
        raise InstallPluginError(_INVALID) from error


def validate_runtime_tree(
    root: Path,
    manifest: RuntimeTreeManifest,
    *,
    apply_permissions: bool,
) -> CacheIdentity:
    """Validate exact membership, identities, permissions, and content."""
    try:
        root_before = identity(root.lstat())
        files, directories = _scan(root)
        expected_files = tuple(entry.path for entry in manifest.entries if entry.type == "file")
        expected_directories = tuple(
            entry.path for entry in manifest.entries if entry.type == "directory"
        )
        if files != expected_files or directories != expected_directories:
            _fail(_INVALID)
        if not secure_path(root, directory=True, apply=apply_permissions):
            _fail(_INVALID)
        for entry in manifest.entries:
            path = root.joinpath(*entry.path.split("/"))
            directory = entry.type == "directory"
            if not secure_path(path, directory=directory, apply=apply_permissions):
                _fail(_INVALID)
            if directory:
                continue
            data = read_source(path, _INVALID, open_direct_file)
            mode = stat.S_IMODE(path.stat().st_mode)
            if (
                len(data) != entry.size
                or hashlib.sha256(data).hexdigest() != entry.sha256
                or (
                    os.name == "posix"
                    and mode != (_EXECUTABLE_MODE if entry.executable else _FILE_MODE)
                )
            ):
                _fail(_INVALID)
        root_after = identity(root.lstat())
    except OSError as error:
        raise InstallPluginError(_INVALID) from error
    if root_after != root_before:
        _fail(_INVALID)
    return root_after


def _parse_entry(raw: dict[str, bool | int | str]) -> RuntimeEntry:
    if set(raw) != set(_MEMBER_KEYS):
        _fail(_MANIFEST_INVALID)
    path = raw.get("path")
    size = raw.get("size")
    digest = raw.get("sha256")
    kind = raw.get("type")
    executable = raw.get("executable")
    if (
        not isinstance(path, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not isinstance(digest, str)
        or not isinstance(kind, str)
        or not isinstance(executable, bool)
        or not _normalized(path)
        or size < 0
        or len(digest) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in digest)
        or kind not in {"directory", "file"}
        or (kind == "directory" and (size != 0 or digest != _EMPTY_SHA256 or executable))
    ):
        _fail(_MANIFEST_INVALID)
    return RuntimeEntry(path, size, digest, kind, executable)


def _load_exclusions(
    path: Path,
    expected_sha256: str,
    expected_count: int,
) -> tuple[ArchiveExclusion, ...]:
    data = read_source(path, _MANIFEST_INVALID, open_direct_file)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        _fail(_MANIFEST_INVALID)
    try:
        raw = cast("list[dict[str, bool | int | str]]", json.loads(data))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail(_MANIFEST_INVALID)
    entries = tuple(_parse_entry(item) for item in raw)
    paths = tuple(entry.path for entry in entries)
    if (
        len(entries) != expected_count
        or paths != tuple(sorted(set(paths), key=str.encode))
        or any(
            entry.type != "file" or PurePosixPath(entry.path).suffix != ".pyc" for entry in entries
        )
    ):
        _fail(_MANIFEST_INVALID)
    return tuple(
        ArchiveExclusion(entry.path, entry.size, entry.sha256, entry.executable)
        for entry in entries
    )


def _scan(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    files: list[str] = []
    directories: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as iterator:
            for entry in iterator:
                path = Path(entry.path)
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & reparse
                ):
                    _fail(_INVALID)
                if stat.S_ISDIR(metadata.st_mode):
                    directories.append(relative)
                    stack.append(path)
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    files.append(relative)
                else:
                    _fail(_INVALID)
    return tuple(sorted(files, key=str.encode)), tuple(sorted(directories, key=str.encode))


def _directories(paths: Iterable[str]) -> tuple[str, ...]:
    directories: set[str] = set()
    for path in paths:
        parts = path.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    return tuple(sorted(directories, key=str.encode))


def _normalized(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and "\\" not in path and not pure.is_absolute() and ".." not in pure.parts


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
