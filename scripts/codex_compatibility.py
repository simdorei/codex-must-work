"""Validate every direct Codex runtime before installer mutation."""

from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING, Final, Never

from scripts.codex_compatibility_policy import inspect_managed_policy
from scripts.codex_compatibility_types import (
    CompatibilityResult,
    FileSnapshot,
    MarketplaceSnapshot,
    RuntimeSnapshot,
)
from scripts.codex_marketplace_probe import root_marketplace_snapshot
from scripts.codex_runtime_discovery import discover_runtimes
from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from pathlib import Path

ALLOWED_CODEX_RELEASES: Final = {
    "0.144.0-alpha.4": "049586f41571e74b44c841868bca3a2233214a71",
    "0.144.0": "767822446c7a594caa19609ca435281a9ec67e0d",
    "0.145.0-alpha.18": "f84f9a6406cc55b210395f71b4c6aed236fc7ebb",
}
_TIMEOUT_SECONDS: Final = 10.0
_RUN_COMMAND = subprocess.run

__all__ = [
    "ALLOWED_CODEX_RELEASES",
    "CompatibilityResult",
    "FileSnapshot",
    "MarketplaceSnapshot",
    "RuntimeSnapshot",
    "preflight_codex_compatibility",
    "validate_codex_compatibility",
]


def validate_codex_compatibility(
    codex_home: Path,
    source_root: Path,
    *,
    require_plugins: bool = False,
    expected: CompatibilityResult | None = None,
) -> CompatibilityResult:
    """Return a complete fail-closed compatibility snapshot."""
    home = _absolute_directory(codex_home, "codex_home_invalid")
    source = _absolute_directory(source_root, "unsafe_source_root")
    files, runtime_paths, selected = discover_runtimes(home)
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    runtimes: list[RuntimeSnapshot] = []
    policies = None
    for runtime in runtime_paths:
        version = _version(runtime, env)
        hooks, plugins = _features(runtime, env)
        if not hooks:
            _fail("codex_hooks_disabled")
        if require_plugins and not plugins:
            _fail("codex_plugins_disabled")
        current_policy = inspect_managed_policy(home, version)
        if policies is None:
            policies = current_policy
        elif policies != current_policy:
            _fail("managed_hook_policy_unverifiable")
        runtimes.append(RuntimeSnapshot(runtime, version, hooks, plugins))
    marketplaces = tuple(
        root_marketplace_snapshot(runtime, source, env, _invoke) for runtime in runtime_paths
    )
    result = CompatibilityResult(files, tuple(runtimes), policies or (), marketplaces, selected)
    if expected is not None and _stable(result) != _stable(expected):
        _fail(_changed_reason(result, expected))
    return result


preflight_codex_compatibility = validate_codex_compatibility


def _version(runtime: Path, env: dict[str, str]) -> str:
    reason = "unsupported_codex_hook_contract"
    result = _invoke((str(runtime), "--version"), env, reason)
    match = re.fullmatch(r"codex-cli ([^\s]+)\r?\n?", result.stdout)
    if result.returncode or result.stderr or match is None:
        _fail(reason)
    version = match.group(1)
    if version not in ALLOWED_CODEX_RELEASES:
        _fail(reason)
    return version


def _features(runtime: Path, env: dict[str, str]) -> tuple[bool, bool]:
    result = _invoke((str(runtime), "features", "list"), env, "codex_hooks_disabled")
    if result.returncode:
        _fail("codex_hooks_disabled")
    rows: dict[str, list[bool]] = {"hooks": [], "plugins": []}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"\s*(hooks|plugins)(?:\s+\S+)+\s+(true|false)\s*", line)
        if match is not None:
            rows[match.group(1)].append(match.group(2) == "true")
    if any(len(values) != 1 for values in rows.values()):
        _fail("codex_hooks_disabled")
    return rows["hooks"][0], rows["plugins"][0]


def _stable(result: CompatibilityResult) -> tuple[object, ...]:
    runtimes = tuple((item.path, item.version, item.hooks_enabled) for item in result.runtimes)
    return result.files, runtimes, result.policies, result.marketplaces, result.selected_executable


def _changed_reason(current: CompatibilityResult, expected: CompatibilityResult) -> str:
    if current.policies != expected.policies:
        return "managed_hook_policy_unverifiable"
    if any(not item.hooks_enabled for item in current.runtimes):
        return "codex_hooks_disabled"
    return "codex_runtime_changed"


def _invoke(
    argv: tuple[str, ...], env: dict[str, str], reason: str
) -> subprocess.CompletedProcess[str]:
    try:
        return _run_command(argv, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallPluginError(reason) from error


def _run_command(
    argv: tuple[str, ...], *, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return _RUN_COMMAND(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=_TIMEOUT_SECONDS,
        check=False,
        shell=False,
    )


def _absolute_directory(path: Path, reason: str) -> Path:
    try:
        absolute = path.absolute()
        if not path.is_absolute() or path.resolve(strict=True) != absolute or not absolute.is_dir():
            _fail(reason)
    except (OSError, RuntimeError):
        _fail(reason)
    return absolute


def _fail(reason: str) -> Never:
    detail = (
        "CMW must be updated for this Codex version"
        if reason == "unsupported_codex_hook_contract"
        else None
    )
    raise InstallPluginError(reason, detail)
