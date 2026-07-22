from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import cache_publication, install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.codex_compatibility import CompatibilityResult
from scripts.config_publication import write_config_bytes as real_write_config_bytes
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_lock import installer_lock
from tests.install_plugin_support import (
    CACHE_PUBLICATION_FAILED,
    HOOKS_DISABLED,
    compatibility_fixture,
    publisher,
    source_fixture,
    trusted_states,
    unsafe_prior_config,
)

if TYPE_CHECKING:
    from scripts.config_metadata import ConfigSnapshot
    from scripts.installer_lock import InstallerLease

pytest_plugins = ("tests.install_plugin_fixtures",)


@pytest.mark.parametrize("variant", ["zero", "incomplete", "malformed"])
def test_unsafe_enabled_prior_is_compare_safely_disabled_without_touching_other_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    prior_source = tmp_path / "unqualified-prior"
    prior_source.mkdir()
    raw = unsafe_prior_config(prior_source.resolve(), variant)
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    compatibility = compatibility_fixture(home)

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    def fail_publish(*_args: object, **_kwargs: object) -> CachePublication:
        raise InstallPluginError(CACHE_PUBLICATION_FAILED)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", fail_publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)

    result = install(home.resolve(), source)

    expected = raw.replace(b"enabled = true # target", b"enabled = false # target", 1)
    assert not result.install_ok
    assert result.final_plugin_disabled is True
    assert config.read_bytes() == expected


def test_external_writer_conflict_preserves_writer_bytes_then_best_effort_disables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    prior_source = tmp_path / "unqualified-prior"
    prior_source.mkdir()
    raw = unsafe_prior_config(prior_source.resolve(), "zero")
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    compatibility = compatibility_fixture(home)
    raced = False

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    def race_write(
        lease: InstallerLease,
        expected: ConfigSnapshot,
        replacement: bytes,
    ) -> bytes:
        nonlocal raced
        if not raced:
            raced = True
            external = expected.data.replace(b'marker = "preserve"', b'marker = "external"')
            _ = expected.path.write_bytes(external)
        return real_write_config_bytes(lease, expected, replacement)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(installer_observation, "write_config_bytes", race_write, raising=False)

    result = install(home.resolve(), source)
    final = config.read_bytes()

    assert raced
    assert not result.install_ok
    assert result.external_config_conflict_after_failure is True
    assert result.final_plugin_disabled is True
    assert b'marker = "external"' in final
    assert b"enabled = false # target" in final


@pytest.mark.parametrize("prior_valid", [True, False])
def test_failed_upgrade_restores_only_a_fully_validated_prior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_valid: bool,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    old_source = source_fixture(tmp_path, "1.0.0", "source-old")
    new_source = source_fixture(tmp_path, "2.0.0", "source-new")
    compatibility = compatibility_fixture(home)

    def check_ok(*_args: object, **_kwargs: object) -> CompatibilityResult:
        return compatibility

    def snapshot(path: Path) -> tuple[CacheIdentity, str]:
        metadata = path.stat()
        return CacheIdentity(metadata.st_dev, metadata.st_ino), f"digest:{path.name}"

    def retained(path: Path, expected: CacheIdentity, digest: str) -> bool:
        metadata = path.stat()
        return (
            prior_valid
            and CacheIdentity(metadata.st_dev, metadata.st_ino) == expected
            and digest == f"digest:{path.name}"
        )

    def trusted(path: Path, marketplace: str) -> tuple[TrustedHookState, ...]:
        assert marketplace == "codex-must-work-local"
        return trusted_states(path)

    def remove(path: Path, expected: CacheIdentity) -> None:
        metadata = path.stat()
        assert CacheIdentity(metadata.st_dev, metadata.st_ino) == expected
        path.rmdir()

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check_ok)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(installer_observation, "snapshot_retained_cache", snapshot)
    monkeypatch.setattr(installer_observation, "retained_cache_matches", retained)
    monkeypatch.setattr(installer_observation, "trusted_hook_states_for_plugin", trusted)
    monkeypatch.setattr(cache_publication, "remove_tree", remove)
    first = install(home.resolve(), old_source)
    assert first.install_ok
    prior_bytes = (home / "config.toml").read_bytes()

    calls = 0

    def fail_upgrade(*_args: object, **_kwargs: object) -> CompatibilityResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InstallPluginError(HOOKS_DISABLED)
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", fail_upgrade)
    second = install(home.resolve(), new_source)
    with installer_lock(home.resolve()) as lease:
        final = installer_observation.observe_config(home.resolve(), lease)

    assert not second.install_ok
    assert second.created_cache_removed is True
    assert final.plugin_disabled is (not prior_valid)
    if prior_valid:
        assert (home / "config.toml").read_bytes() == prior_bytes
        assert final.source_root is not None
        assert final.source_root.name == "1.0.0"
        assert second.final_cache_matches_enabled_trust is True
    else:
        assert final.source_root is not None
        assert final.source_root.name == "2.0.0"
        assert second.final_cache_matches_enabled_trust is False
