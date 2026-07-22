from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import cache_publication, install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.codex_compatibility import CompatibilityResult
from scripts.codex_config import update_codex_config as real_update_codex_config
from scripts.hook_trust import read_plugin_manifest
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_observation import ConfigObservation
from scripts.installer_result import InstallResult
from tests.install_plugin_support import (
    HOOKS_DISABLED,
    compatibility_fixture,
    publication_fixture,
    publisher,
    source_fixture,
    trusted_states,
)

if TYPE_CHECKING:
    from scripts.codex_config import ConfigMutation
    from scripts.installer_lock import InstallerLease

pytest_plugins = ("tests.install_plugin_fixtures",)


def test_initial_install_orders_disabled_before_cache_and_two_revalidations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    events: list[str] = []
    compatibility = compatibility_fixture(home)

    def check(*_args: object, **kwargs: object) -> CompatibilityResult:
        events.append(f"compatibility:{kwargs.get('require_plugins')}")
        return compatibility

    def publish(*_args: object, **_kwargs: object) -> CachePublication:
        assert not (home / "config.toml").exists()
        events.append("publish_cache")
        return publication_fixture(home)

    def validate(publication: CachePublication, source_fixture: Path) -> tuple[CacheIdentity, str]:
        config = home / "config.toml"
        enabled: bool | None = None
        if config.exists():
            raw = config.read_text(encoding="utf-8")
            plugin_block = raw.split(
                '[plugins."codex-must-work@codex-must-work-local"]', maxsplit=1
            )[1].split("\n[", maxsplit=1)[0]
            enabled = "enabled = true\n" in plugin_block
            if enabled is False:
                assert raw.count('[hooks.state."codex-must-work@codex-must-work-local:') == 3
        events.append(f"validate_cache:{enabled}")
        return publication.identity, publication.digest

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(install_plugin, "validate_cache_publication", validate, raising=False)
    monkeypatch.setattr(installer_observation, "validate_cache_publication", validate)

    result = install(home.resolve(), source)

    assert isinstance(result, InstallResult)
    assert result.install_ok
    assert result.final_plugin_disabled is False
    assert result.final_cache_matches_enabled_trust is True
    assert events == [
        "compatibility:False",
        "publish_cache",
        "validate_cache:None",
        "compatibility:True",
        "validate_cache:False",
        "validate_cache:True",
        "compatibility:True",
        "validate_cache:True",
        "validate_cache:True",
    ]


def test_disabled_trust_must_be_exact_before_compatibility_or_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    compatibility_requires_plugins = 0
    enabled_mutations: list[bool] = []
    original_observe = installer_observation.observe_config

    def check(*_args: object, **kwargs: object) -> CompatibilityResult:
        nonlocal compatibility_requires_plugins
        if kwargs.get("require_plugins") is True:
            compatibility_requires_plugins += 1
        return compatibility

    def update(
        codex_home: Path,
        mutation: ConfigMutation,
        lease: InstallerLease | None = None,
    ) -> bytes:
        enabled_mutations.append(mutation.plugin_enabled)
        return real_update_codex_config(codex_home, mutation, lease)

    def observe(path: Path, lease: InstallerLease) -> ConfigObservation:
        observed = original_observe(path, lease)
        if (
            observed.plugin_present
            and observed.plugin_disabled
            and observed.source_root is not None
            and len(observed.trusted_hooks) == 3
        ):
            return installer_observation.ConfigObservation(
                observed.snapshot,
                observed.plugin_present,
                observed.plugin_disabled,
                observed.legacy_enabled,
                observed.source_root,
                observed.trusted_hooks[:-1],
            )
        return observed

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(install_plugin, "update_codex_config", update)
    monkeypatch.setattr(install_plugin, "observe_config", observe)
    monkeypatch.setattr(installer_observation, "observe_config", observe)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert compatibility_requires_plugins == 0
    assert True not in enabled_mutations


def test_real_full_tree_corruption_before_enable_never_publishes_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_cache_validation: None,
) -> None:
    _ = real_cache_validation
    home = tmp_path / "home"
    home.mkdir()
    source = Path(__file__).resolve().parents[1]
    compatibility = compatibility_fixture(home)
    corrupted = False
    enabled_mutations: list[bool] = []

    def check(*_args: object, **kwargs: object) -> CompatibilityResult:
        nonlocal corrupted
        if kwargs.get("require_plugins") is True and not corrupted:
            version = read_plugin_manifest(source).version
            cached = (
                home
                / "plugins"
                / "cache"
                / "codex-must-work-local"
                / "codex-must-work"
                / version
                / "README.md"
            )
            _ = cached.write_bytes(b"corrupted-before-enable")
            corrupted = True
        return compatibility

    def update(
        codex_home: Path,
        mutation: ConfigMutation,
        lease: InstallerLease | None = None,
    ) -> bytes:
        enabled_mutations.append(mutation.plugin_enabled)
        return real_update_codex_config(codex_home, mutation, lease)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "update_codex_config", update)

    result = install(home.resolve(), source)

    assert corrupted
    assert not result.install_ok
    assert True not in enabled_mutations


@pytest.mark.parametrize("failure_call", [2, 3])
def test_pre_enable_or_post_enable_revalidation_failure_disables_and_removes_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    publication: CachePublication | None = None
    calls = 0

    def check(*_args: object, **_kwargs: object) -> CompatibilityResult:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise InstallPluginError(HOOKS_DISABLED)
        return compatibility

    def publish(*_args: object, **_kwargs: object) -> CachePublication:
        nonlocal publication
        publication = publication_fixture(home)
        return publication

    def remove(path: Path, identity: CacheIdentity) -> None:
        metadata = path.stat()
        assert CacheIdentity(metadata.st_dev, metadata.st_ino) == identity
        path.rmdir()

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(cache_publication, "remove_tree", remove)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.error_code == "codex_plugins_disabled"
    assert result.final_plugin_disabled is True
    assert result.final_cache_matches_enabled_trust is False
    assert result.created_cache_removed is True
    expected = home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / "1.2.3"
    assert not expected.exists()


def test_already_disabled_config_is_byte_identical_until_cache_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    original = b'[plugins."codex-must-work@codex-must-work-local"]\nenabled = false\n'
    _ = (home / "config.toml").write_bytes(original)
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)

    def publish(*_args: object, **_kwargs: object) -> CachePublication:
        assert (home / "config.toml").read_bytes() == original
        return publication_fixture(home)

    def check(*args: object, **kwargs: object) -> CompatibilityResult:
        _ = args, kwargs
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    result = install(home.resolve(), source)
    assert result.install_ok
