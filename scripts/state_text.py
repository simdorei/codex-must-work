"""Small text operations for private plugin state."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from scripts.state_io import (
    StateError,
    ensure_direct_regular_file,
    open_direct_file,
    safe_absolute_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def read_private_text(root: Path, path: Path, *, max_bytes: int) -> str:
    """Read bounded ASCII state without following a redirected final path."""
    if max_bytes < 1:
        message = "max_bytes must be positive"
        raise ValueError(message)
    root_absolute, path_absolute = safe_absolute_path(root, path)
    ensure_direct_regular_file(root_absolute, path_absolute)
    descriptor = open_direct_file(path_absolute, os.O_RDONLY)
    try:
        payload = os.read(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(payload) > max_bytes:
        message = "state text exceeds its size limit"
        raise StateError(message)
    try:
        return payload.decode("ascii")
    except UnicodeDecodeError as error:
        message = "state text is not ASCII"
        raise StateError(message) from error


def write_private_text(root: Path, path: Path, value: str) -> None:
    """Write a small private marker without following a final redirect."""
    root_absolute, path_absolute = safe_absolute_path(root, path)
    descriptor = open_direct_file(path_absolute, os.O_CREAT | os.O_WRONLY)
    try:
        payload = value.encode("ascii")
        os.ftruncate(descriptor, 0)
        _ = os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    ensure_direct_regular_file(root_absolute, path_absolute)
    if os.name != "nt":
        path_absolute.chmod(0o600, follow_symlinks=False)
