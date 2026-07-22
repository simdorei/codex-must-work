from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import install_plugin, installer_observation
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.codex_compatibility import CompatibilityResult
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.installer_result import InstallResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.synchronize import Event as EventType


HOOKS_DISABLED = "codex_plugins_disabled"
UNSUPPORTED = "unsupported_codex_hook_contract"
CACHE_PUBLICATION_FAILED = "cache_publication_failed"
CACHE_CLEANUP_FAILED = "cache_cleanup_failed"
LOCK_FAILED = "installer_lock_failed"
PACKAGE_HOOKS_INVALID = "package_hooks_invalid"
CONFIG_PUBLICATION_FAILED = "codex_config_publication_failed"
INJECTED_READ_FAILURE = "injected final read failure"


@dataclass(frozen=True, slots=True)
class ContendedArgs:
    home: str
    source: str
    temp_root: str
    entered: EventType
    release: EventType
    hold: bool
    result_path: str


def contended_install(values: ContendedArgs) -> None:
    tempfile.tempdir = values.temp_root

    def fail(*args: object, **kwargs: object) -> CompatibilityResult:
        _ = args, kwargs
        values.entered.set()
        if values.hold:
            assert values.release.wait(30)
        raise InstallPluginError(UNSUPPORTED)

    setattr(install_plugin, "validate_codex_compatibility", fail)  # noqa: B010
    result = install(Path(values.home), Path(values.source))
    _ = Path(values.result_path).write_text(result.error_code or "none", encoding="utf-8")


def source_fixture(tmp_path: Path, version: str = "1.2.3", directory: str = "source") -> Path:
    source = tmp_path / directory
    (source / ".codex-plugin").mkdir(parents=True)
    (source / "hooks").mkdir()
    _ = (source / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-must-work", "version": version}), encoding="utf-8"
    )
    _ = (source / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    return source.resolve()


def compatibility_fixture(home: Path) -> CompatibilityResult:
    executable = home / "fake-codex"
    return CompatibilityResult.for_tests(executable)


def publication_fixture(home: Path, version: str = "1.2.3") -> CachePublication:
    path = home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / version
    path.mkdir(parents=True, exist_ok=True)
    identity = path.stat()
    return CachePublication(
        cache_path=path,
        digest="a" * 64,
        created_by_run=True,
        identity=CacheIdentity(identity.st_dev, identity.st_ino),
    )


def publisher(home: Path) -> Callable[[Path, Path, str], CachePublication]:
    def publish(source_fixture: Path, _home: Path, version: str) -> CachePublication:
        return publication_fixture(home, version)

    return publish


def trusted_states(source_fixture: Path) -> tuple[TrustedHookState, ...]:
    labels = (
        "session_start",
        "user_prompt_submit",
        "stop",
    )
    prefix = "codex-must-work@codex-must-work-local:hooks/hooks.json"
    return tuple(
        TrustedHookState(f"{prefix}:{label}:0:0", f"sha256:{index:064x}")
        for index, label in enumerate(labels)
    )


def unsafe_prior_config(source: Path, variant: str) -> bytes:
    lines = [
        'marker = "preserve"',
        "",
        "[marketplaces.codex-must-work-local]",
        'source_type = "local"',
        f"source = {json.dumps(str(source))}",
        "",
        '[plugins."codex-must-work@codex-must-work-local"]',
        "enabled = true # target",
    ]
    states = trusted_states(source)
    if variant == "incomplete":
        for state in states[:-1]:
            lines.extend(
                (
                    "",
                    f'[hooks.state."{state.key}"]',
                    "enabled = true",
                    f'trusted_hash = "{state.trusted_hash}"',
                )
            )
    elif variant == "malformed":
        lines.extend(
            (
                "",
                f'[hooks.state."{states[0].key}"]',
                'enabled = "not-a-boolean"',
                "trusted_hash = 7",
            )
        )
    return ("\n".join(lines) + "\n").encode()


def failure_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, CompatibilityResult]:
    home = tmp_path / "home"
    home.mkdir()
    source = source_fixture(tmp_path)
    compatibility = compatibility_fixture(home)

    def check(*args: object, **kwargs: object) -> CompatibilityResult:
        _ = args, kwargs
        return compatibility

    monkeypatch.setattr(install_plugin, "validate_codex_compatibility", check)
    monkeypatch.setattr(install_plugin, "trusted_states", trusted_states)
    return home, source, compatibility


def assert_failed_without_success(result: InstallResult, captured_out: str) -> None:
    assert not result.install_ok
    assert isinstance(result.final_plugin_disabled, bool)
    assert isinstance(result.final_cache_matches_enabled_trust, bool)
    assert isinstance(result.created_cache_removed, bool)
    assert "install=ok" not in captured_out
