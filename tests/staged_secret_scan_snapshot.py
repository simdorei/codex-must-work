from __future__ import annotations

import hashlib
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from tests.staged_secret_scan_git import ScanError, run_git

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

INDEX_UNREADABLE: Final[str] = "INDEX_UNREADABLE"
MALFORMED_GIT_OUTPUT: Final[str] = "MALFORMED_GIT_OUTPUT"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """An immutable copy of the command-start index and isolated Git object sink."""

    identifier: str
    environment: Mapping[str, str]


def git_path(
    root: Path,
    name: str,
    environment: Mapping[str, str] | None = None,
) -> Path:
    try:
        text = (
            run_git(
                root,
                "rev-parse",
                "--git-path",
                name,
                environment=environment,
            )
            .decode("utf-8")
            .strip()
        )
    except UnicodeDecodeError as error:
        raise ScanError(MALFORMED_GIT_OUTPUT) from error
    if not text or "\n" in text or "\r" in text:
        raise ScanError(MALFORMED_GIT_OUTPUT)
    path = Path(text)
    return path if path.is_absolute() else root / path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def capture_snapshot(root: Path) -> Generator[Snapshot]:
    """Copy the real index and isolate any write-tree objects outside the repository."""
    real_index = git_path(root, "index")
    real_objects = git_path(root, "objects")
    with TemporaryDirectory(prefix="cmw-staged-snapshot-") as temporary:
        temporary_root = Path(temporary)
        if temporary_root.is_relative_to(root):
            raise ScanError(INDEX_UNREADABLE)
        copied_index = temporary_root / "index"
        isolated_objects = temporary_root / "objects"
        isolated_objects.mkdir()
        try:
            _ = shutil.copyfile(real_index, copied_index)
        except OSError as error:
            raise ScanError(INDEX_UNREADABLE) from error
        environment = MappingProxyType(
            {
                "GIT_INDEX_FILE": str(copied_index),
                "GIT_OBJECT_DIRECTORY": str(isolated_objects),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(real_objects),
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
        )
        yield Snapshot(_sha256(copied_index), environment)
