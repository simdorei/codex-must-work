"""Capture one source package into a private immutable install candidate."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.cache_package import load_package
from scripts.cache_security import create_secure_directory, read_source, write_package
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from collections.abc import Generator


@contextmanager
def package_candidate_snapshot(source_root: Path) -> Generator[Path]:
    """Yield exact source bytes from an isolated tree and always remove them."""
    package = load_package(source_root, _read_direct)
    with tempfile.TemporaryDirectory(prefix="cmw-package-candidate-") as directory:
        root = create_secure_directory(Path(directory).resolve() / "package")
        write_package(root, package)
        yield root


def _read_direct(path: Path, reason: str) -> bytes:
    return read_source(path, reason, open_direct_file)
