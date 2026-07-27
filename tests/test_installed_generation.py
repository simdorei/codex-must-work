from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import installed_generation
from scripts.cache_types import CacheIdentity
from scripts.config_metadata import ConfigSnapshot
from scripts.hook_trust import PluginManifest, TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.installed_generation import (
    InstalledGeneration,
    configured_generation,
    require_session_generation,
    select_generation,
)
from scripts.installer_observation import ConfigObservation, PriorState


def _generation(version: str, digest: str = "a" * 64) -> InstalledGeneration:
    return InstalledGeneration(
        version=version,
        root=Path(f"/cache/{version}"),
        digest=digest,
        identity=CacheIdentity(1, 2),
    )


def _prior(root: Path, *, restorable: bool = True, legacy: bool | None = None) -> PriorState:
    trust = (TrustedHookState("hook", "sha256:" + "a" * 64),)
    observation = ConfigObservation(
        snapshot=ConfigSnapshot(b"", None, None, root / "config.toml", (1, 1)),
        plugin_present=True,
        plugin_disabled=False,
        legacy_enabled=legacy,
        source_root=root,
        trusted_hooks=trust,
    )
    return PriorState(observation, restorable, CacheIdentity(1, 2), "a" * 64)


@dataclass(frozen=True, slots=True)
class _EligibilityCase:
    restorable: bool
    legacy: bool | None
    retained: bool
    trust_matches: bool
    eligible: bool


def test_newer_requested_generation_wins_over_older_configured_generation() -> None:
    # Given
    configured = _generation("1.2.3")
    requested = _generation("1.3.0")

    # When
    selected = select_generation(configured, requested)

    # Then
    assert selected is requested


def test_newer_configured_generation_prevents_requested_downgrade() -> None:
    # Given
    configured = _generation("2.0.0")
    requested = _generation("1.9.9")

    # When
    selected = select_generation(configured, requested)

    # Then
    assert selected is configured


def test_same_version_same_content_is_idempotent() -> None:
    # Given
    configured = _generation("1.2.3")
    requested = _generation("1.2.3")

    # When
    selected = select_generation(configured, requested)

    # Then
    assert selected is configured


def test_same_version_different_content_is_a_generation_conflict() -> None:
    # Given
    configured = _generation("1.2.3")
    requested = _generation("1.2.3", "b" * 64)

    # When / Then
    with pytest.raises(InstallPluginError, match="installed_generation_conflict"):
        _ = select_generation(configured, requested)


@pytest.mark.parametrize("version", ["local", "1.2", "v2", "01.2.3"])
def test_noncanonical_versions_are_ineligible(version: str) -> None:
    # Given
    requested = _generation(version)

    # When / Then
    with pytest.raises(InstallPluginError, match="installed_generation_version_invalid"):
        _ = select_generation(None, requested)


@pytest.mark.parametrize(
    "case",
    [
        _EligibilityCase(
            restorable=False,
            legacy=None,
            retained=True,
            trust_matches=True,
            eligible=False,
        ),
        _EligibilityCase(
            restorable=True,
            legacy=True,
            retained=True,
            trust_matches=True,
            eligible=False,
        ),
        _EligibilityCase(
            restorable=True,
            legacy=None,
            retained=False,
            trust_matches=True,
            eligible=False,
        ),
        _EligibilityCase(
            restorable=True,
            legacy=None,
            retained=True,
            trust_matches=False,
            eligible=False,
        ),
        _EligibilityCase(
            restorable=True,
            legacy=None,
            retained=True,
            trust_matches=True,
            eligible=True,
        ),
    ],
)
def test_configured_generation_requires_the_complete_enabled_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _EligibilityCase,
) -> None:
    # Given
    root = tmp_path / "0.2.0+codex.20260722081644"
    root.mkdir()
    prior = _prior(root, restorable=case.restorable, legacy=case.legacy)
    expected_trust = prior.observation.trusted_hooks if case.trust_matches else ()

    def retained(_root: Path, _identity: CacheIdentity, _digest: str) -> bool:
        return case.retained

    def manifest(_root: Path) -> PluginManifest:
        return PluginManifest("codex-must-work", root.name, "hooks/hooks.json")

    def trust(_root: Path, _marketplace: str) -> tuple[TrustedHookState, ...]:
        return expected_trust

    monkeypatch.setattr(installed_generation, "retained_cache_matches", retained)
    monkeypatch.setattr(installed_generation, "read_plugin_manifest", manifest)
    monkeypatch.setattr(installed_generation, "trusted_hook_states_for_plugin", trust)

    # When
    result = configured_generation(prior)

    # Then
    assert (result is not None) is case.eligible
    if result is not None:
        assert result.root == root


def test_session_generation_requires_the_exact_resolved_plugin_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    home = tmp_path / "home"
    home.mkdir()
    configured_root = tmp_path / "configured"
    configured_root.mkdir()
    other_root = tmp_path / "other"
    other_root.mkdir()
    expected = InstalledGeneration(
        "0.2.0+codex.20260722081644",
        configured_root.resolve(),
        "a" * 64,
        CacheIdentity(1, 2),
    )

    def configured(_prior: PriorState) -> InstalledGeneration:
        return expected

    monkeypatch.setattr(installed_generation, "configured_generation", configured)

    # When
    exact = require_session_generation(home.resolve(), configured_root)

    # Then
    assert exact == expected
    with pytest.raises(InstallPluginError, match="installed_generation_mismatch"):
        _ = require_session_generation(home.resolve(), other_root)


def test_session_generation_accepts_verified_simdorei_marketplace_cache(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    root = (
        home / "plugins" / "cache" / "simdorei" / "codex-must-work" / "0.2.0+codex.20260728010101"
    )
    package_files = (
        ".codex-plugin/plugin.json",
        "hooks/hooks.json",
        "runtime/package-files.json",
    )
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    (root / "runtime").mkdir()
    _ = (root / ".codex-plugin/plugin.json").write_text(
        '{"name":"codex-must-work","version":"0.2.0+codex.20260728010101"}',
        encoding="utf-8",
    )
    _ = (root / "hooks/hooks.json").write_text('{"hooks":{}}', encoding="utf-8")
    _ = (root / "runtime/package-files.json").write_text(
        json.dumps(package_files),
        encoding="utf-8",
    )

    generation = require_session_generation(home.resolve(), root)

    assert generation.root == root.resolve()
    assert generation.version == root.name
    assert len(generation.digest) == 64


def test_session_generation_rejects_unapproved_marketplace_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / "plugins" / "cache" / "other" / "codex-must-work" / "0.2.0"
    root.mkdir(parents=True)

    with pytest.raises(InstallPluginError, match="installed_generation_mismatch"):
        _ = require_session_generation(home.resolve(), root)
