from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import codex_compatibility
from scripts import codex_compatibility_policy as policy_module
from scripts.codex_compatibility import (
    ALLOWED_CODEX_RELEASES,
    CompatibilityResult,
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

def test_allowed_release_contract_is_source_pinned() -> None:
    assert ALLOWED_CODEX_RELEASES == ALLOWED


def test_supported_runtime_preflight_is_direct_complete_and_path_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    selected = bundle_fixture(home, ".sandbox-bin")
    unselected = bundle_fixture(home, "plugins/.plugin-appserver")
    source = source_fixture(tmp_path)
    calls = fake_commands(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "poisoned-path"))

    result = validate_codex_compatibility(home.resolve(), source, require_plugins=True)

    assert isinstance(result, CompatibilityResult)
    assert result.selected_executable == selected
    assert {runtime.path for runtime in result.runtimes} == {selected, unselected}
    assert all(runtime.version == "0.144.0" for runtime in result.runtimes)
    assert all(runtime.hooks_enabled and runtime.plugins_enabled for runtime in result.runtimes)
    assert all(Path(call[0][0]).is_absolute() for call in calls)
    assert all(call[2]["CODEX_HOME"] != str(source) for call in calls)


@pytest.mark.parametrize(
    ("version", "reason"),
    [
        ("0.146.0", "unsupported_codex_hook_contract"),
        ("malformed", "unsupported_codex_hook_contract"),
    ],
)
def test_unsupported_or_malformed_version_fails_before_target_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    reason: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ = bundle_fixture(home, ".sandbox-bin")
    before = tuple(home.rglob("*"))
    _ = fake_commands(monkeypatch, version=version)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source_fixture(tmp_path))
    assert caught.value.reason_code == reason
    assert tuple(home.rglob("*")) == before


def test_unselected_runtime_with_disabled_hooks_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    selected = bundle_fixture(home, ".sandbox-bin")
    unselected = bundle_fixture(home, "plugins/.plugin-appserver")
    source = source_fixture(tmp_path)

    def run(
        argv: tuple[str, ...], *, env: dict[str, str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        _ = env, cwd
        if argv[1:] == ("--version",):
            return subprocess.CompletedProcess(argv, 0, "codex-cli 0.144.0\n", "")
        if argv[1:] == ("features", "list"):
            hooks = Path(argv[0]) != unselected
            return subprocess.CompletedProcess(
                argv, 0, f"hooks stable {str(hooks).lower()}\nplugins experimental true\n", ""
            )
        output = json.dumps(
            [
                {
                    "name": "codex-must-work",
                    "marketplace": "codex-must-work-local",
                    "source": {"source": "local", "path": "./"},
                }
            ]
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(codex_compatibility, "_run_command", run)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source, require_plugins=True)
    assert caught.value.reason_code == "codex_hooks_disabled"
    assert selected != unselected


def test_mixed_supported_and_unsupported_runtime_candidates_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    selected = bundle_fixture(home, ".sandbox-bin")
    unsupported = bundle_fixture(home, "plugins/.plugin-appserver")
    source = source_fixture(tmp_path)

    def run(
        argv: tuple[str, ...], *, env: dict[str, str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        _ = env, cwd
        if argv[1:] == ("--version",):
            version = "0.146.0" if Path(argv[0]) == unsupported else "0.144.0"
            return subprocess.CompletedProcess(argv, 0, f"codex-cli {version}\n", "")
        if argv[1:] == ("features", "list"):
            return subprocess.CompletedProcess(
                argv, 0, "hooks stable true\nplugins experimental true\n", ""
            )
        raise AssertionError

    monkeypatch.setattr(codex_compatibility, "_run_command", run)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source, require_plugins=True)
    assert caught.value.reason_code == "unsupported_codex_hook_contract"
    assert selected != unsupported


def test_runtime_digest_swap_is_rejected_by_exact_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    executable = bundle_fixture(home, ".sandbox-bin")
    source = source_fixture(tmp_path)
    _ = fake_commands(monkeypatch)
    initial = validate_codex_compatibility(home.resolve(), source, require_plugins=True)
    _ = executable.write_bytes(b"replacement")
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(
            home.resolve(), source, require_plugins=True, expected=initial
        )
    assert caught.value.reason_code == "codex_runtime_changed"


def test_root_marketplace_failure_cleans_preflight_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ = bundle_fixture(home, ".sandbox-bin")
    source = source_fixture(tmp_path)
    observed: list[Path] = []

    def run(
        argv: tuple[str, ...], *, env: dict[str, str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        _ = cwd
        if argv[1:] == ("--version",):
            return subprocess.CompletedProcess(argv, 0, "codex-cli 0.144.0\n", "")
        if argv[1:] == ("features", "list"):
            return subprocess.CompletedProcess(
                argv, 0, "hooks stable true\nplugins experimental true\n", ""
            )
        observed.append(Path(env["CODEX_HOME"]))
        return subprocess.CompletedProcess(argv, 1, "", "parse failed")

    monkeypatch.setattr(codex_compatibility, "_run_command", run)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source, require_plugins=True)
    assert caught.value.reason_code == "unsupported_codex_marketplace_root"
    assert observed
    assert all(not path.exists() for path in observed)
