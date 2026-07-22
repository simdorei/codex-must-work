from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts import install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication


@pytest.fixture(autouse=True)
def isolated_installer_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if not request.module.__name__.startswith("tests.test_install_plugin"):
        return
    root = tmp_path / "installer-temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))

    if "real_cache_validation" in request.fixturenames:
        return

    def validate(
        publication: CachePublication,
        _source: Path,
    ) -> tuple[CacheIdentity, str]:
        return publication.identity, publication.digest

    monkeypatch.setattr(install_plugin, "validate_cache_publication", validate, raising=False)
    monkeypatch.setattr(installer_observation, "validate_cache_publication", validate)


@pytest.fixture
def real_cache_validation() -> None:
    pass
