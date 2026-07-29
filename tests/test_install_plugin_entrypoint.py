from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import install_plugin
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from tests.install_plugin_support import (
    CACHE_PUBLICATION_FAILED,
    UNSUPPORTED,
    InstallerCallValue,
    compatibility_fixture,
    failure_case,
    publisher,
    source_fixture,
    trusted_states,
)

if TYPE_CHECKING:
    from scripts.cache_types import CachePublication
    from scripts.codex_compatibility import CompatibilityResult

pytest_plugins = ("tests.install_plugin_fixtures",)


def test_direct_script_entrypoint_reaches_cli_usage_from_an_unrelated_working_directory(
    tmp_path: Path,
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "install_plugin.py"
    completed = subprocess.run(  # noqa: S603 -- current Python and repository-owned script
        [sys.executable, str(script)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "usage: install_plugin.py CODEX_HOME SOURCE_ROOT\n"


@pytest.mark.parametrize("failure", ["preflight", "publication"])
def test_data_root_is_created_only_after_compatibility_and_removed_on_failure(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(tmp_path)
    home = tmp_path / f"home-{failure}"
    home.mkdir()
    data_root = home / "plugins" / "data" / "codex-must-work-simdorei"
    checked = False

    def check(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CompatibilityResult:
        nonlocal checked
        assert not data_root.exists()
        checked = True
        if failure == "preflight":
            raise InstallPluginError(UNSUPPORTED)
        return compatibility

    def fail_publish(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CachePublication:
        assert data_root.is_dir()
        raise InstallPluginError(CACHE_PUBLICATION_FAILED)

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", fail_publish)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    result = install(home.resolve(), source)

    assert checked
    assert not result.install_ok
    assert not data_root.exists()


def test_success_creates_exact_private_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_install_receipt: None,
) -> None:
    _ = synthetic_install_receipt
    home, source, compatibility = failure_case(tmp_path, monkeypatch)

    def check(*_args: InstallerCallValue, **_kwargs: InstallerCallValue) -> CompatibilityResult:
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    result = install(home.resolve(), source)

    data_root = home / "plugins" / "data" / "codex-must-work-simdorei"
    assert result.install_ok
    assert data_root.is_dir()
    assert (data_root / ".private-root-v1").is_file()
