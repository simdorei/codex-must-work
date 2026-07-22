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

@pytest.mark.parametrize("kind", ["orphan", "directory", "hardlink", "symlink"])
def test_incomplete_or_unsafe_direct_runtime_fails_before_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    codex_name, _host_name = binary_names()
    if kind == "orphan":
        executable = bundle_fixture(home, "plugins/.plugin-appserver", host=False)
    elif kind == "directory":
        root = home / ".sandbox-bin"
        root.mkdir()
        executable = root / codex_name
        executable.mkdir()
    else:
        executable = bundle_fixture(home, ".sandbox-bin")
        if kind == "hardlink":
            os.link(executable, executable.with_suffix(".alias"))
        else:
            outside = tmp_path / "outside"
            _ = outside.write_bytes(b"outside")
            executable.unlink()
            try:
                executable.symlink_to(outside)
            except OSError:
                pytest.skip("host does not permit file symlink creation")
    calls = fake_commands(monkeypatch)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source_fixture(tmp_path))
    assert caught.value.reason_code in {"codex_runtime_incomplete", "codex_runtime_unsafe"}
    assert calls == []


@pytest.mark.parametrize("change", ["add", "remove", "host-swap"])
def test_runtime_set_or_host_identity_change_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ = bundle_fixture(home, ".sandbox-bin")
    source = source_fixture(tmp_path)
    _ = fake_commands(monkeypatch)
    initial = validate_codex_compatibility(home.resolve(), source)
    if change == "add":
        _ = bundle_fixture(home, "plugins/.plugin-appserver")
    elif change == "remove":
        codex_name, host_name = binary_names()
        (home / ".sandbox-bin" / codex_name).unlink()
        (home / ".sandbox-bin" / host_name).unlink()
        _ = bundle_fixture(home, "plugins/.plugin-appserver")
    else:
        _codex_name, host_name = binary_names()
        _ = (home / ".sandbox-bin" / host_name).write_bytes(b"host replacement")
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(home.resolve(), source, expected=initial)
    assert caught.value.reason_code == "codex_runtime_changed"


@pytest.mark.parametrize(
    "mode",
    ["version-nonzero", "version-multiline", "timeout", "duplicate-hooks", "plugins-false"],
)
def test_command_contract_failures_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _ = bundle_fixture(home, ".sandbox-bin")

    def run(
        argv: tuple[str, ...], *, env: dict[str, str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        _ = env, cwd
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        if argv[1:] == ("--version",):
            output = (
                "codex-cli 0.144.0\nextra\n"
                if mode == "version-multiline"
                else "codex-cli 0.144.0\n"
            )
            code = 1 if mode == "version-nonzero" else 0
            return subprocess.CompletedProcess(argv, code, output, "")
        if argv[1:] == ("features", "list"):
            hooks = (
                "hooks stable true\nhooks stable true\n"
                if mode == "duplicate-hooks"
                else "hooks stable true\n"
            )
            plugins = "false" if mode == "plugins-false" else "true"
            return subprocess.CompletedProcess(
                argv, 0, f"{hooks}plugins experimental {plugins}\n", ""
            )
        pytest.fail("marketplace must not run after a command-contract failure")

    monkeypatch.setattr(codex_compatibility, "_run_command", run)
    with pytest.raises(InstallPluginError) as caught:
        _ = validate_codex_compatibility(
            home.resolve(), source_fixture(tmp_path), require_plugins=mode == "plugins-false"
        )
    expected = (
        "codex_plugins_disabled"
        if mode == "plugins-false"
        else "unsupported_codex_hook_contract"
        if mode in {"version-nonzero", "version-multiline", "timeout"}
        else "codex_hooks_disabled"
    )
    assert caught.value.reason_code == expected

