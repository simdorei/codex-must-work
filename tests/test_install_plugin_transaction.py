from __future__ import annotations

from typing import TYPE_CHECKING

from scripts import cache_publication, install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_mcp_runtime import McpRuntimePublication
from scripts.installer_observation import ConfigObservation, PriorState
from tests.install_plugin_support import (
    CACHE_PUBLICATION_FAILED,
    HOOKS_DISABLED,
    InstallerCallValue,
    compatibility_fixture,
    failure_case,
    publication_fixture,
    publisher,
    source_fixture,
    trusted_states,
    unsafe_prior_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from scripts.codex_compatibility import CompatibilityResult
    from scripts.installer_lock import InstallerLease

pytest_plugins = ("tests.install_plugin_fixtures",)


def test_post_enable_failure_preserves_preexisting_legacy_enabled_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    original = b'[plugins."codex-must-work@simdorei"]\nenabled = true # preserve legacy\n'
    _ = config.write_bytes(original)
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    calls = 0

    def check(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CompatibilityResult:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise InstallPluginError(HOOKS_DISABLED)
        return compatibility

    def remove(path: Path, expected: CacheIdentity) -> None:
        assert CacheIdentity(path.stat().st_dev, path.stat().st_ino) == expected
        path.rmdir()

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(cache_publication, "remove_tree", remove)
    result = install(home.resolve(), source)

    assert not result.install_ok
    assert b"enabled = true # preserve legacy" in config.read_bytes()


def test_disabled_publication_requires_the_local_plugin_table_to_be_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, source, compatibility = failure_case(tmp_path, monkeypatch)
    real_observe = installer_observation.observe_config
    require_plugins_calls = 0

    def check(*_args: InstallerCallValue, **kwargs: InstallerCallValue) -> CompatibilityResult:
        nonlocal require_plugins_calls
        if kwargs.get("require_plugins") is True:
            require_plugins_calls += 1
        return compatibility

    def absent(path: Path, lease: InstallerLease) -> ConfigObservation:
        observed = real_observe(path, lease)
        if observed.source_root is not None:
            return ConfigObservation(
                snapshot=observed.snapshot,
                plugin_present=False,
                plugin_disabled=observed.plugin_disabled,
                legacy_enabled=observed.legacy_enabled,
                source_root=observed.source_root,
                trusted_hooks=observed.trusted_hooks,
            )
        return observed

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "observe_config", absent)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert require_plugins_calls == 0


def test_disabled_state_is_refenced_immediately_before_cache_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    target = home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / "1.2.3"
    raw = unsafe_prior_config(target.resolve(), "zero").replace(
        b"enabled = true # target", b"enabled = false # target"
    )
    config = home / "config.toml"
    _ = config.write_bytes(raw)
    compatibility = compatibility_fixture(home)
    original_initial = install_plugin.initial_disabled_observation
    published = False

    def check(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CompatibilityResult:
        return compatibility

    def race(lease: InstallerLease, prior: PriorState) -> ConfigObservation:
        observed = original_initial(lease, prior)
        _ = config.write_bytes(
            config.read_bytes().replace(b"enabled = false # target", b"enabled = true # target")
        )
        return observed

    def publish(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CachePublication:
        nonlocal published
        published = True
        return publication_fixture(home)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "initial_disabled_observation", race)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    result = install(home.resolve(), source)

    assert not result.install_ok
    assert not published


def test_external_legacy_reenable_before_final_publication_prevents_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    _ = config.write_bytes(b'[plugins."codex-must-work@simdorei"]\nenabled = false # legacy\n')
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    calls = 0

    def check(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CompatibilityResult:
        nonlocal calls
        calls += 1
        if calls == 3:
            _ = config.write_bytes(
                config.read_bytes().replace(b"enabled = false # legacy", b"enabled = true # legacy")
            )
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    result = install(home.resolve(), source)

    assert not result.install_ok
    assert b"enabled = true # legacy" in config.read_bytes()


def test_failed_install_removes_runtime_created_by_same_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    home, source, _compatibility = failure_case(tmp_path, monkeypatch)
    runtime_paths: list[Path] = []

    def prepare(_source: Path, data: Path) -> McpRuntimePublication:
        runtime_path = data / "portable-python-test"
        runtime_paths.append(runtime_path)
        runtime_path.mkdir()
        executable = runtime_path / "python.exe"
        _ = executable.write_bytes(b"runtime")
        metadata = runtime_path.stat()
        return McpRuntimePublication(
            runtime_path,
            CacheIdentity(metadata.st_dev, metadata.st_ino),
            created_by_run=True,
        )

    def fail_publish(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CachePublication:
        raise InstallPluginError(CACHE_PUBLICATION_FAILED)

    monkeypatch.setattr(install_plugin, "prepare_mcp_runtime", prepare, raising=False)
    monkeypatch.setattr(install_plugin, "publish_cache", fail_publish)

    # When
    result = install(home.resolve(), source)

    # Then
    assert not result.install_ok
    assert len(runtime_paths) == 1
    assert not runtime_paths[0].exists()
    assert not (home / "plugins" / "data" / "codex-must-work-codex-must-work-local").exists()
