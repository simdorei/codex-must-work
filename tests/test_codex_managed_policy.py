from __future__ import annotations

import json
import os
import subprocess
from base64 import b64encode
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import codex_compatibility
from scripts import codex_compatibility_policy as policy_module
from scripts.codex_compatibility import (
    ALLOWED_CODEX_RELEASES,
    validate_codex_compatibility,
)
from scripts.codex_compatibility_policy import (
    MANAGED_SOURCE_KIND_ORDER,
    MANAGED_SOURCE_KINDS_BY_RELEASE,
    MANAGED_SOURCE_ORDER,
    MANAGED_SOURCE_SEARCH_BY_RELEASE,
    PolicySourceSpec,
)
from scripts.install_errors import InstallPluginError
from tests.codex_compatibility_support import (
    ALLOWED,
    binary_names,
    bundle_fixture,
    cloud_cache,
    fake_commands,
    policy_spec_provider,
    source_fixture,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_managed_search_order_is_frozen_for_all_allowed_releases() -> None:
    assert set(MANAGED_SOURCE_SEARCH_BY_RELEASE) == set(ALLOWED)
    assert all(order == MANAGED_SOURCE_ORDER for order in MANAGED_SOURCE_SEARCH_BY_RELEASE.values())
    assert MANAGED_SOURCE_KINDS_BY_RELEASE == dict.fromkeys(ALLOWED, MANAGED_SOURCE_KIND_ORDER)


def test_release_specs_use_codex_home_cloud_cache_for_both_logical_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setattr(policy_module, "_platform_name", lambda: "linux", raising=False)
    specs = policy_module.policy_source_specs(home, "0.145.0-alpha.18")
    by_name = {spec.name: spec for spec in specs}
    expected = home / "cloud-config-bundle-cache.json"
    assert by_name["cloud_config"].path == expected
    assert by_name["cloud_requirements"].path == expected


def test_windows_system_policy_ignores_programdata_environment_spoof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    known = (tmp_path / "known-program-data").resolve()
    spoof = (tmp_path / "spoofed-program-data").resolve()
    monkeypatch.setenv("PROGRAMDATA", str(spoof))
    monkeypatch.setattr(policy_module, "_platform_name", lambda: "windows", raising=False)
    monkeypatch.setattr(policy_module, "_windows_program_data", lambda: known, raising=False)
    specs = policy_module.policy_source_specs(home, "0.144.0")
    by_name = {spec.name: spec for spec in specs}
    assert by_name["system_config"].path == known / "OpenAI" / "Codex" / "config.toml"
    system_config = by_name["system_config"].path
    assert system_config is not None
    assert spoof not in system_config.parents


def test_macos_managed_preferences_are_logical_sources_with_digest_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    values = {
        "config_toml_base64": b64encode(b"[features]\nhooks = true\n").decode(),
        "requirements_toml_base64": b64encode(b"allow_managed_hooks_only = false\n").decode(),
    }
    monkeypatch.setattr(policy_module, "_platform_name", lambda: "darwin", raising=False)

    def preference(key: str) -> str | None:
        return values.get(key)

    monkeypatch.setattr(policy_module, "_managed_preference", preference, raising=False)
    snapshots = policy_module.inspect_managed_policy(home, "0.145.0-alpha.18")
    by_name = {item.name: item for item in snapshots}
    assert by_name["mdm_config"].present
    assert by_name["mdm_config"].identity is None
    assert by_name["mdm_config"].digest is not None
    assert by_name["mdm_config"].location == "cfpreferences:com.openai.codex:config_toml_base64"
    assert by_name["mdm_requirements"].present


def test_cloud_bundle_raw_managed_hook_denial_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    _ = (home / "cloud-config-bundle-cache.json").write_bytes(
        cloud_cache(config="[features]\nhooks = false\n")
    )
    monkeypatch.setattr(policy_module, "_platform_name", lambda: "linux", raising=False)
    with pytest.raises(InstallPluginError) as caught:
        _ = policy_module.inspect_managed_policy(home, "0.145.0-alpha.18")
    assert caught.value.reason_code == "managed_hook_policy_unverifiable"


@pytest.mark.parametrize(
    "payload",
    [
        cloud_cache(),
        b'{"signed_payload":{"version":1}}',
        b'{"signed_payload":{"version":2},"signature":"forged"}',
        b'{"signed_payload":{"version":1,"expires_at":"expired"},"signature":"forged"}',
        b'{"signed_payload":[],"signature":"forged"}',
    ],
)
def test_present_cloud_cache_is_unverifiable_without_auth_bound_hmac_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    cache = home / "cloud-config-bundle-cache.json"
    monkeypatch.setattr(policy_module, "_platform_name", lambda: "linux", raising=False)
    _ = cache.write_bytes(payload)
    with pytest.raises(InstallPluginError) as caught:
        _ = policy_module.inspect_managed_policy(home, "0.145.0-alpha.18")
    assert caught.value.reason_code == "managed_hook_policy_unverifiable"


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        ("allow_managed_hooks_only = true\n", "managed_hooks_only"),
        ("[features]\nhooks = false\n", "codex_hooks_disabled"),
        ("[feature_requirements]\nhooks = false\n", "codex_hooks_disabled"),
        ('allow_managed_hooks_only = "false"\n', "managed_hook_policy_unverifiable"),
        ('[features]\nhooks = "false"\n', "managed_hook_policy_unverifiable"),
        ('[feature_requirements]\nhooks = "false"\n', "managed_hook_policy_unverifiable"),
    ],
)
def test_managed_only_or_disabled_policy_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
    reason: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ = bundle_fixture(home, ".sandbox-bin")
    managed = tmp_path / "managed.toml"
    _ = managed.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(policy_module, "policy_source_specs", policy_spec_provider(managed))
    _ = fake_commands(monkeypatch)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source_fixture(tmp_path))
    assert caught.value.reason_code == reason


def test_opaque_cloud_policy_fails_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ = bundle_fixture(home, ".sandbox-bin")
    managed = tmp_path / "cloud"
    _ = managed.write_text("[features]\nhooks = true\n", encoding="utf-8")
    monkeypatch.setattr(
        policy_module,
        "policy_source_specs",
        policy_spec_provider(managed, opaque=True),
    )
    _ = fake_commands(monkeypatch)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source_fixture(tmp_path))
    assert caught.value.reason_code == "managed_hook_policy_unverifiable"


@pytest.mark.parametrize("change", ["add", "change", "remove"])
def test_policy_add_change_remove_is_detected_on_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ = bundle_fixture(home, ".sandbox-bin")
    managed = tmp_path / "requirements.toml"
    if change in {"change", "remove"}:
        _ = managed.write_text("[features]\nhooks = true\n", encoding="utf-8")
    monkeypatch.setattr(policy_module, "policy_source_specs", policy_spec_provider(managed))
    _ = fake_commands(monkeypatch)
    source = source_fixture(tmp_path)
    initial = validate_codex_compatibility(home.resolve(), source)
    if change == "add":
        _ = managed.write_text("[features]\nhooks = true\n", encoding="utf-8")
    elif change == "change":
        _ = managed.write_text("[features]\nhooks = true\n# changed\n", encoding="utf-8")
    else:
        managed.unlink()
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source, expected=initial)
    assert caught.value.reason_code == "managed_hook_policy_unverifiable"
