"""Validate full identity-bound immutable installer caches."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scripts.cache_package import load_package
from scripts.cache_security import read_source, validate_tree
from scripts.install_errors import InstallPluginError
from scripts.state_io import open_direct_file

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.cache_types import CacheIdentity, CachePublication

_DIGEST_MISMATCH: Final = "cache_digest_mismatch"
_IDENTITY_MISMATCH: Final = "cache_identity_mismatch"


def validate_cache_publication(
    publication: CachePublication, source_root: Path
) -> tuple[CacheIdentity, str]:
    """Match a publication to the exact source manifest and full tree."""
    expected = load_package(source_root, _read_direct)
    if expected.digest != publication.digest:
        raise InstallPluginError(_DIGEST_MISMATCH)
    identity = validate_tree(
        publication.cache_path,
        expected,
        lambda root: load_package(root, _read_direct),
    )
    if identity != publication.identity:
        raise InstallPluginError(_IDENTITY_MISMATCH)
    return identity, expected.digest


def snapshot_retained_cache(root: Path) -> tuple[CacheIdentity, str]:
    """Capture and validate one retained cache's exact manifest tree."""
    expected = load_package(root, _read_direct)
    identity = validate_tree(root, expected, lambda path: load_package(path, _read_direct))
    return identity, expected.digest


def retained_cache_matches(root: Path, identity: CacheIdentity, digest: str) -> bool:
    """Revalidate a retained cache against its captured identity and digest."""
    try:
        current_identity, current_digest = snapshot_retained_cache(root)
    except (OSError, InstallPluginError):
        return False
    return current_identity == identity and current_digest == digest


def _read_direct(path: Path, reason: str) -> bytes:
    return read_source(path, reason, open_direct_file)
