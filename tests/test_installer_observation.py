from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts.install_errors import InstallPluginError
from scripts.installer_lock import installer_lock
from scripts.installer_observation import observe_config

if TYPE_CHECKING:
    from pathlib import Path


def _cache_generation(home: Path, version: str, manifest_version: str | None = None) -> Path:
    generation = home / "plugins" / "cache" / "simdorei" / "codex-must-work" / version
    manifest = generation / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    _ = manifest.write_text(
        json.dumps(
            {
                "name": "codex-must-work",
                "version": version if manifest_version is None else manifest_version,
            }
        ),
        encoding="utf-8",
    )
    return generation


def test_observe_config_selects_highest_valid_direct_semver_cache(tmp_path: Path) -> None:
    # Given: canonical config and several direct cache generations.
    home = tmp_path / "home"
    home.mkdir()
    _ = _cache_generation(home, "1.0.9")
    expected = _cache_generation(home, "1.0.10")
    _ = _cache_generation(home, "9.0.0", manifest_version="8.0.0")
    _ = _cache_generation(home, "not-semver")
    _ = (home / "config.toml").write_text(
        (
            "[marketplaces.simdorei]\n"
            'source_type = "git"\n'
            'source = "https://github.com/simdorei/codex-must-work.git"\n'
            "\n"
            '[plugins."codex-must-work@simdorei"]\n'
            "enabled = true\n"
            "\n"
            '[plugins."codex-must-work@codex-must-work-local"]\n'
            "enabled = false\n"
        ),
        encoding="utf-8",
    )

    # When: the exact config snapshot is observed under its installer lease.
    with installer_lock(home) as lease:
        observed = observe_config(home.resolve(), lease)

    # Then: only the highest valid direct manifest-backed semver cache is selected.
    assert observed.source_root == expected.resolve()
    assert observed.plugin_present
    assert not observed.plugin_disabled
    assert observed.legacy_enabled is False


def test_observe_config_requires_canonical_git_marketplace_identity(tmp_path: Path) -> None:
    # Given: an enabled canonical plugin and a lookalike marketplace source.
    home = tmp_path / "home"
    home.mkdir()
    _ = _cache_generation(home, "2.0.0")
    _ = (home / "config.toml").write_text(
        (
            "[marketplaces.simdorei]\n"
            'source_type = "git"\n'
            'source = "https://example.invalid/simdorei/codex-must-work.git"\n'
            'ref = "main"\n'
            "\n"
            '[plugins."codex-must-work@simdorei"]\n'
            "enabled = true\n"
        ),
        encoding="utf-8",
    )

    # When: the config is observed.
    with installer_lock(home) as lease:
        observed = observe_config(home.resolve(), lease)

    # Then: plugin flags remain observable but no cache source is trusted.
    assert observed.plugin_present
    assert not observed.plugin_disabled
    assert observed.source_root is None


def test_observe_config_rejects_malformed_toml(tmp_path: Path) -> None:
    # Given: malformed TOML at the installer-owned config boundary.
    home = tmp_path / "home"
    home.mkdir()
    _ = (home / "config.toml").write_text("[plugins\n", encoding="utf-8")

    # When: the malformed snapshot is observed.
    with installer_lock(home) as lease, pytest.raises(InstallPluginError) as caught:
        _ = observe_config(home.resolve(), lease)

    # Then: the public failure reason remains stable and privacy-safe.
    assert str(caught.value) == "codex_config_malformed"
