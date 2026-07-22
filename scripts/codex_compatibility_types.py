"""Immutable values shared by Codex compatibility probes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.codex_compatibility_policy import PolicySnapshot
    from scripts.installer_lock import FileIdentity


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Bind a binary path to its identity and digest."""

    path: Path
    identity: FileIdentity
    digest: str


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Record effective features from one direct runtime."""

    path: Path
    version: str
    hooks_enabled: bool
    plugins_enabled: bool


@dataclass(frozen=True, slots=True)
class MarketplaceSnapshot:
    """Record normalized root-marketplace output."""

    runtime: Path
    digest: str


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    """Carry the immutable preflight authorization snapshot."""

    files: tuple[FileSnapshot, ...]
    runtimes: tuple[RuntimeSnapshot, ...]
    policies: tuple[PolicySnapshot, ...]
    marketplaces: tuple[MarketplaceSnapshot, ...]
    selected_executable: Path

    @classmethod
    def for_tests(cls, executable: Path) -> CompatibilityResult:
        """Build an empty compatibility fixture for transaction tests."""
        return cls((), (), (), (), executable)
