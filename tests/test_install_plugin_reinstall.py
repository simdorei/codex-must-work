from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, final

import pytest

from scripts import install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.control_capability import load_control_key
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_preflight import eligible_no_write
from tests.install_plugin_support import (
    compatibility_fixture,
    publication_fixture,
    source_fixture,
    trusted_states,
    unsafe_prior_config,
)

if TYPE_CHECKING:
    from scripts.codex_compatibility import CompatibilityResult
    from scripts.hook_trust import TrustedHookState
    from scripts.installer_observation import PriorState

pytest_plugins = ("tests.install_plugin_fixtures",)


@final
class _RetainedValidity:
    def __init__(self) -> None:
        self.valid = True

    def matches(self, _root: Path, _identity: CacheIdentity, _digest: str) -> bool:
        return self.valid


def _trusted_for(path: Path, _marketplace: str) -> tuple[TrustedHookState, ...]:
    return trusted_states(path)


def test_same_version_reinstall_preserves_control_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
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
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)

    def publish(_source: Path, _home: Path, _version: str) -> CachePublication:
        return publication_fixture(home)

    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    assert install(home.resolve(), source).install_ok
    plugin_data = home / "plugins" / "data" / "codex-must-work-codex-must-work-local"
    first = load_control_key(plugin_data)
    first_identity = (plugin_data / "control.key").stat()

    # When
    assert install(home.resolve(), source).install_ok

    # Then
    second_identity = (plugin_data / "control.key").stat()
    assert load_control_key(plugin_data) == first
    assert (second_identity.st_dev, second_identity.st_ino) == (
        first_identity.st_dev,
        first_identity.st_ino,
    )


@pytest.mark.parametrize("mode", ["publish-error", "identity", "digest", "deleted"])
def test_no_write_reinstall_race_never_leaves_an_enabled_cache_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    publications = 0
    retained = _RetainedValidity()

    def check(
        _home: Path,
        _source: Path,
        *,
        require_plugins: bool = False,
        expected: CompatibilityResult | None = None,
    ) -> CompatibilityResult:
        _ = require_plugins, expected
        return compatibility

    def publish(_source: Path, _home: Path, _version: str) -> CachePublication:
        nonlocal publications
        publications += 1
        return publication_fixture(home)

    def snapshot(path: Path) -> tuple[CacheIdentity, str]:
        metadata = path.stat()
        return CacheIdentity(metadata.st_dev, metadata.st_ino), "a" * 64

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    monkeypatch.setattr(installer_observation, "snapshot_retained_cache", snapshot)
    monkeypatch.setattr(
        installer_observation,
        "retained_cache_matches",
        retained.matches,
    )
    monkeypatch.setattr(
        installer_observation,
        "trusted_hook_states_for_plugin",
        _trusted_for,
    )
    assert install(home.resolve(), source).install_ok
    target = home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / "1.2.3"
    if mode == "deleted":
        original_eligible = eligible_no_write

        def delete_after_classification(
            prior: PriorState,
            expected: Path,
            trust: tuple[TrustedHookState, ...],
        ) -> bool:
            eligible = original_eligible(prior, expected, trust)
            expected.rmdir()
            return eligible

        monkeypatch.setattr(install_plugin, "eligible_no_write", delete_after_classification)

    def raced_validation(
        publication: CachePublication, _source_fixture: Path
    ) -> tuple[CacheIdentity, str]:
        retained.valid = False
        if mode in {"publish-error", "deleted"}:
            reason = "cache_same_version_mismatch"
            raise InstallPluginError(reason)
        identity = publication.identity
        if mode == "identity":
            identity = CacheIdentity(identity.device, identity.inode + 1)
        digest = "b" * 64 if mode == "digest" else publication.digest
        return identity, digest

    monkeypatch.setattr(install_plugin, "validate_cache_publication", raced_validation)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.final_plugin_disabled is True
    assert publications == 1
    if mode == "deleted":
        assert result.created_cache_removed is False
        assert not target.exists()


def test_malformed_legacy_enabled_value_never_qualifies_for_no_write_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    config = home / "config.toml"
    _ = config.write_bytes(
        unsafe_prior_config(source, "zero")
        + b'\n[plugins."codex-must-work@simdorei"]\nenabled = "yes"\n'
    )
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
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)

    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.error_code == "codex_config_unsupported_syntax"


@pytest.mark.parametrize("case", ["wrong-name", "local", "unsafe"])
def test_manifest_and_selection_preflight_do_not_mutate_config_or_cache(
    tmp_path: Path, case: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    original = b'user_marker = "unchanged"\n'
    _ = config.write_bytes(original)
    source = source_fixture(tmp_path)
    root = Path(__file__).resolve().parents[1]
    _ = (source / "hooks" / "hooks.json").write_bytes((root / "hooks" / "hooks.json").read_bytes())
    manifest = {"name": "codex-must-work", "version": "1.2.3"}
    if case == "wrong-name":
        manifest["name"] = "other-plugin"
    elif case == "local":
        manifest["version"] = "local"
    elif case == "unsafe":
        manifest["version"] = "../unsafe"
    _ = (source / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    before = tuple(
        sorted(
            (
                path.relative_to(home).as_posix(),
                path.is_dir(),
                path.read_bytes() if path.is_file() else b"",
            )
            for path in home.rglob("*")
        )
    )

    result = install(home.resolve(), source)
    after = tuple(
        sorted(
            (
                path.relative_to(home).as_posix(),
                path.is_dir(),
                path.read_bytes() if path.is_file() else b"",
            )
            for path in home.rglob("*")
        )
    )

    assert not result.install_ok
    assert config.read_bytes() == original
    assert after == before
