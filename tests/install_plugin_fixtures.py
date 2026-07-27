from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING

import pytest

from scripts import install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.installed_generation import InstalledGeneration
from scripts.installer_mcp_runtime import McpRuntimePublication

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_installer_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if not request.path.name.startswith("test_install_plugin"):
        return
    root = tmp_path / "installer-temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))

    def reuse_runtime(_source: Path, data: Path) -> McpRuntimePublication:
        return McpRuntimePublication(
            data / "portable-python-test",
            CacheIdentity(0, 0),
            created_by_run=False,
        )

    monkeypatch.setattr(install_plugin, "prepare_mcp_runtime", reuse_runtime, raising=False)

    if "real_cache_validation" in request.fixturenames:
        return

    def validate(
        publication: CachePublication,
        _source: Path,
    ) -> tuple[CacheIdentity, str]:
        return publication.identity, publication.digest

    monkeypatch.setattr(install_plugin, "validate_cache_publication", validate, raising=False)
    monkeypatch.setattr(installer_observation, "validate_cache_publication", validate)

    def configured(prior: installer_observation.PriorState) -> InstalledGeneration | None:
        source = prior.observation.source_root
        if source is None or not source.exists() or prior.observation.legacy_enabled is True:
            return None
        metadata = source.stat()
        return InstalledGeneration(
            source.name,
            source,
            "a" * 64,
            CacheIdentity(metadata.st_dev, metadata.st_ino),
        )

    def requested(publication: CachePublication) -> InstalledGeneration:
        return InstalledGeneration(
            publication.cache_path.name,
            publication.cache_path,
            publication.digest,
            publication.identity,
        )

    monkeypatch.setattr(install_plugin, "configured_generation", configured, raising=False)
    monkeypatch.setattr(install_plugin, "requested_generation", requested, raising=False)


@pytest.fixture
def real_cache_validation() -> None:
    pass
