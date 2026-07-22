"""Shared immutable values for cache publication."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import os
    from pathlib import Path


class CacheIdentity(NamedTuple):
    """Stable filesystem identity for one cache object."""

    device: int
    inode: int


class CachePublication(NamedTuple):
    """Result consumed by the enclosing installer transaction."""

    cache_path: Path
    digest: str
    created_by_run: bool
    identity: CacheIdentity


class Package(NamedTuple):
    """Manifest order, immutable bytes, and framed package digest."""

    paths: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    digest: str


def identity(metadata: os.stat_result) -> CacheIdentity:
    """Reduce host metadata to the identity used across rename boundaries."""
    return CacheIdentity(metadata.st_dev, metadata.st_ino)
