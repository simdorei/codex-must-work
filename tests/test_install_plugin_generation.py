from __future__ import annotations

import hashlib
import shutil
from typing import TYPE_CHECKING

from scripts import (
    cache_publication,
    install_plugin,
    installer_observation_config,
    installer_prior_observation,
)
from scripts.cache_types import CacheIdentity
from scripts.codex_config import update_codex_config as real_update_codex_config
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_lock import installer_lock
from scripts.installer_observation import classify_prior, prior_cache_still_valid
from tests.install_plugin_support import (
    compatibility_fixture,
    publisher,
    source_fixture,
    trusted_states,
    unsafe_prior_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from scripts.codex_compatibility import CompatibilityResult
    from scripts.codex_config import ConfigMutation
    from scripts.hook_trust import TrustedHookState
    from scripts.installer_lock import InstallerLease

pytest_plugins = ("tests.install_plugin_fixtures",)

_OLDER = "0.1.0+codex.20260721000000"
_CURRENT = "0.2.0+codex.20260722081644"
_INCOMPLETE = "0.3.0+codex.20260723000000"
_CORRUPT = "0.4.0+codex.20260724000000"
_PUBLICATION_FAILED = "codex_config_publication_failed"


def _allow_install(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> CompatibilityResult:
    compatibility = compatibility_fixture(home)

    def check(
        _home: Path,
        _source: Path,
        *,
        require_plugins: bool = False,
        expected: CompatibilityResult | None = None,
    ) -> CompatibilityResult:
        _ = require_plugins, expected
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)

    def snapshot(path: Path) -> tuple[CacheIdentity, str]:
        metadata = path.stat()
        return CacheIdentity(metadata.st_dev, metadata.st_ino), "a" * 64

    def retained(path: Path, identity: CacheIdentity, digest: str) -> bool:
        metadata = path.stat()
        return identity == CacheIdentity(metadata.st_dev, metadata.st_ino) and digest == "a" * 64

    monkeypatch.setattr(installer_prior_observation, "snapshot_retained_cache", snapshot)
    monkeypatch.setattr(installer_prior_observation, "retained_cache_matches", retained)

    def trust(path: Path, _marketplace: str) -> tuple[TrustedHookState, ...]:
        return trusted_states(path)

    monkeypatch.setattr(installer_prior_observation, "trusted_hook_states_for_plugin", trust)
    return compatibility


def _enabled_prior_config(source: Path) -> bytes:
    raw = unsafe_prior_config(source, "zero")
    hooks = "".join(
        (f'\n[hooks.state."{state.key}"]\nenabled = true\ntrusted_hash = "{state.trusted_hash}"\n')
        for state in trusted_states(source)
    )
    return raw + hooks.encode()


def test_newer_requested_generation_wins_without_scanning_ambient_higher_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_install_receipt: None,
) -> None:
    _ = synthetic_install_receipt
    # Given
    home = tmp_path / "home"
    home.mkdir()
    cache_base = home / "plugins" / "cache" / "simdorei" / "codex-must-work"
    configured = source_fixture(cache_base, _OLDER, _OLDER)
    requested = source_fixture(tmp_path, _CURRENT, "requested")
    original = _enabled_prior_config(configured)
    _ = (home / "config.toml").write_bytes(original)
    incomplete = cache_base / _INCOMPLETE
    corrupt = cache_base / _CORRUPT
    incomplete.mkdir(parents=True)
    corrupt.mkdir()
    _ = (corrupt / ".codex-plugin").mkdir()
    _ = (corrupt / ".codex-plugin" / "plugin.json").write_bytes(b"{")
    _ = _allow_install(monkeypatch, home)

    # When
    result = install(home.resolve(), requested)

    # Then
    with installer_lock(home.resolve()) as lease:
        final = installer_observation_config.observe_config(home.resolve(), lease)
    assert result.install_ok
    assert final.source_root == (cache_base / _CURRENT).resolve()
    assert final.source_root not in {incomplete.resolve(), corrupt.resolve()}
    assert incomplete.is_dir()
    assert (corrupt / ".codex-plugin" / "plugin.json").read_bytes() == b"{"


def test_older_requested_generation_never_downgrades_the_configured_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_install_receipt: None,
) -> None:
    _ = synthetic_install_receipt
    # Given
    home = tmp_path / "home"
    home.mkdir()
    cache_base = home / "plugins" / "cache" / "simdorei" / "codex-must-work"
    configured = source_fixture(cache_base, _CURRENT, _CURRENT)
    requested = source_fixture(tmp_path, _OLDER, "requested")
    original = _enabled_prior_config(configured)
    _ = (home / "config.toml").write_bytes(original)
    _ = _allow_install(monkeypatch, home)

    # When
    result = install(home.resolve(), requested)

    # Then
    assert result.install_ok
    assert (home / "config.toml").read_bytes() == original


def test_publication_crash_restores_qualified_prior_config_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_install_receipt: None,
) -> None:
    _ = synthetic_install_receipt
    # Given
    home = tmp_path / "home"
    home.mkdir()
    cache_base = home / "plugins" / "cache" / "simdorei" / "codex-must-work"
    configured = source_fixture(cache_base, _OLDER, _OLDER)
    requested = source_fixture(tmp_path, _CURRENT, "requested")
    original = _enabled_prior_config(configured)
    config = home / "config.toml"
    _ = config.write_bytes(original)
    _ = _allow_install(monkeypatch, home)

    publications = 0

    def crash_after_disabled(
        codex_home: Path,
        mutation: ConfigMutation,
        lease: InstallerLease | None = None,
    ) -> bytes:
        nonlocal publications
        publications += 1
        if publications == 2:
            raise InstallPluginError(_PUBLICATION_FAILED)
        return real_update_codex_config(codex_home, mutation, lease)

    monkeypatch.setattr("scripts.install_plugin.update_codex_config", crash_after_disabled)

    def remove(path: Path, expected: CacheIdentity) -> None:
        metadata = path.stat()
        assert expected == CacheIdentity(metadata.st_dev, metadata.st_ino)
        shutil.rmtree(path)

    monkeypatch.setattr(cache_publication, "remove_tree", remove)
    with installer_lock(home.resolve()) as lease:
        prior = classify_prior(home.resolve(), lease)
    assert prior.restorable_enabled
    assert prior_cache_still_valid(prior)
    before_hash = hashlib.sha256(original).hexdigest()

    # When
    result = install(home.resolve(), requested)

    # Then
    final = config.read_bytes()
    assert not result.install_ok
    assert result.error_code == _PUBLICATION_FAILED
    assert hashlib.sha256(final).hexdigest() == before_hash
    assert final == original
