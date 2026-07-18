"""Resolve a trusted Codex binary without searching the workspace."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override

from scripts.state_io import ensure_existing_components_are_direct

_MISSING_EXECUTABLE: Final = "trusted_codex_executable_missing"
_UNSAFE_HOME: Final = "trusted_codex_home_invalid"
_DIGEST_MISMATCH: Final = "trusted_codex_executable_changed"


@dataclass(frozen=True, slots=True)
class CodexExecutableError(OSError):
    """Report why the resident manager cannot launch Codex."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


def resolve_codex_executable(expected_sha256: str | None = None) -> Path:
    """Return the CODEX_HOME-owned direct executable path."""
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    if not codex_home.is_absolute():
        raise CodexExecutableError(_UNSAFE_HOME)
    name = "codex.exe" if os.name == "nt" else "codex"
    path = _verified(codex_home / ".sandbox-bin" / name)
    if expected_sha256 is not None and _executable_sha256(path) != expected_sha256:
        raise CodexExecutableError(_DIGEST_MISMATCH)
    return path


def _executable_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))  # noqa: PTH100
    ensure_existing_components_are_direct(Path(absolute.anchor), absolute)
    if not absolute.is_file() or absolute.stat().st_nlink != 1:
        raise CodexExecutableError(_MISSING_EXECUTABLE)
    return absolute
