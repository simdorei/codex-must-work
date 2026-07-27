from __future__ import annotations

from typing import TYPE_CHECKING

from scripts import install_plugin, install_plugin_cli
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from tests.install_plugin_support import (
    HOOKS_DISABLED,
    compatibility_fixture,
    publisher,
    source_fixture,
    trusted_states,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from scripts.codex_compatibility import CompatibilityResult
    from scripts.installer_result import InstallResult

pytest_plugins = ("tests.install_plugin_fixtures",)


def test_external_config_conflict_never_masks_the_primary_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)
    config = home / "config.toml"
    calls = 0

    def check(
        _home: Path,
        _source: Path,
        *,
        require_plugins: bool = False,
        expected: CompatibilityResult | None = None,
    ) -> CompatibilityResult:
        nonlocal calls
        _ = require_plugins, expected
        calls += 1
        if calls == 2:
            _ = config.write_bytes(config.read_bytes() + b'external_marker = "preserve"\n')
            raise InstallPluginError(HOOKS_DISABLED)
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    result = install(home.resolve(), source)

    assert not result.install_ok
    assert result.error_code == "external_config_conflict_after_failure"
    assert result.primary_error_code == HOOKS_DISABLED
    assert result.cleanup_error_code == "codex_config_concurrent_change"
    assert result.external_config_conflict_after_failure is True

    def completed(codex_home: Path, source_root: Path) -> InstallResult:
        _ = codex_home, source_root
        return result

    assert install_plugin_cli.run_cli(completed, ["home", "source"]) == 1
    serialized = capsys.readouterr().err
    assert f'"primary_error_code": "{HOOKS_DISABLED}"' in serialized
    assert '"cleanup_error_code": "codex_config_concurrent_change"' in serialized
