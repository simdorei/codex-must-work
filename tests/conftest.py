from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.cache_types import CacheIdentity, identity

if TYPE_CHECKING:
    from scripts.runtime_tree import RuntimeTreeManifest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def private_root_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def secure(root: Path) -> None:
        root.mkdir(exist_ok=True)

    monkeypatch.setattr("scripts.setup.ensure_private_root", secure)
    monkeypatch.setattr("scripts.manager_reuse.ensure_private_root", secure, raising=False)
    monkeypatch.setattr("scripts.hook_event.ensure_private_root", secure)
    monkeypatch.setattr("scripts.watcher.ensure_private_root", secure)
    monkeypatch.setattr("scripts.calibration_state.ensure_private_root", secure)


@pytest.fixture(autouse=True)
def synthetic_uninstall_runtime_tree(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Keep non-runtime uninstall tests focused on their declared contract."""
    if not request.path.name.startswith("test_uninstall_"):
        return

    def validate(
        root: Path,
        _manifest: RuntimeTreeManifest,
        *,
        apply_permissions: bool,
    ) -> CacheIdentity:
        _ = apply_permissions
        return identity(root.lstat())

    monkeypatch.setattr("scripts.uninstall_paths.validate_runtime_tree", validate)
