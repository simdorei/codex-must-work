"""Shared identity-bound uninstall filesystem records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.cache_types import CacheIdentity


@dataclass(frozen=True, slots=True)
class OwnedRoot:
    """Bind a validated deletion root to its exact filesystem identity."""

    path: Path
    identity: CacheIdentity


@dataclass(frozen=True, slots=True)
class QuarantinedRoot:
    """Bind a moved root to its original and randomized safe names."""

    original: Path
    quarantine: Path
    identity: CacheIdentity
