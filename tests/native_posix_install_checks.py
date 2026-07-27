"""Installed package and configuration checks for the native POSIX smoke."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from datetime import date, datetime
from datetime import time as datetime_time
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from tests.native_posix_smoke_support import (
    CheckName,
    Checks,
    NativeLayout,
    bootstrap_clean,
    run_install,
)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type TomlTable = dict[str, TomlValue]
type TomlValue = (
    str | int | float | bool | datetime | date | datetime_time | list[TomlValue] | TomlTable
)

_PLUGIN_ID: Final = "codex-must-work@codex-must-work-local"
_MARKETPLACE: Final = "codex-must-work-local"


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


class _TomlLoader(Protocol):
    def __call__(self, source: str, /) -> TomlTable: ...


def _json_loader() -> _JsonLoader:
    return json.loads


def _toml_loader() -> _TomlLoader:
    return tomllib.loads


_LOAD_JSON: Final = _json_loader()
_LOAD_TOML: Final = _toml_loader()


def _check(name: str) -> CheckName:
    return CheckName(name)


def cache_membership_exact(cache: Path, package_files: tuple[str, ...]) -> bool:
    """Compare every cache file and directory with manifest-derived membership."""
    expected = set(package_files)
    for relative in package_files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected.add(parent.as_posix())
            parent = parent.parent
    actual = {path.relative_to(cache).as_posix() for path in cache.rglob("*")}
    return actual == expected


def first_install(layout: NativeLayout, home: Path, checks: Checks) -> Path:
    """Install once and validate the immutable cache and merged configuration."""
    config = home / "config.toml"
    _ = config.write_text('marker = "preserve"\n', encoding="utf-8", newline="\n")
    config.chmod(0o600)
    before = config.lstat()
    result = run_install(layout, home)
    checks.record_exit(result.returncode)
    checks.require(result.returncode == 0, _check("first_install_exit"))
    checks.require(result.stdout == "install=ok\n", _check("first_install_stdout_exact"))
    checks.require(result.stderr == "", _check("first_install_stderr_empty"))
    after = config.lstat()
    checks.require(
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino),
        _check("config_atomically_replaced"),
    )
    checks.require(
        'marker = "preserve"' in config.read_text("utf-8"),
        _check("config_prior_data_preserved"),
    )
    cache = _validate_cache(layout, home, checks)
    _validate_config(home, cache, checks)
    data = home / "plugins" / "data" / "codex-must-work-codex-must-work-local"
    checks.require(data.resolve(strict=True) == data, _check("plugin_data_identity_exact"))
    checks.require(stat.S_IMODE(data.lstat().st_mode) == 0o700, _check("plugin_data_mode_exact"))
    stage = home / "plugins" / ".cmw-install-staging" / "codex-must-work"
    checks.require(not stage.exists() or not any(stage.iterdir()), _check("cache_staging_clean"))
    checks.require(not tuple(home.glob(".config.toml.cmw.*")), _check("config_staging_clean"))
    checks.require(bootstrap_clean(layout), _check("first_install_bootstrap_clean"))
    return cache


def _manifest_version(source_root: Path, checks: Checks) -> str:
    parsed = _LOAD_JSON((source_root / ".codex-plugin" / "plugin.json").read_text("utf-8"))
    checks.require(isinstance(parsed, dict), _check("plugin_manifest_object"))
    version = parsed.get("version") if isinstance(parsed, dict) else None
    checks.require(isinstance(version, str), _check("plugin_version_present"))
    return version if isinstance(version, str) else ""


def _validate_cache(layout: NativeLayout, home: Path, checks: Checks) -> Path:
    version = _manifest_version(layout.source_root, checks)
    cache = home / "plugins" / "cache" / _MARKETPLACE / "codex-must-work" / version
    checks.require(cache.resolve(strict=True) == cache, _check("cache_identity_exact"))
    package = _LOAD_JSON((layout.source_root / "runtime" / "package-files.json").read_text("utf-8"))
    checks.require(isinstance(package, list), _check("package_manifest_array"))
    expected = (
        tuple(item for item in package if isinstance(item, str))
        if isinstance(package, list)
        else ()
    )
    checks.require(
        isinstance(package, list) and len(expected) == len(package),
        _check("package_paths_text"),
    )
    checks.require(cache_membership_exact(cache, expected), _check("cache_package_exact"))
    for path in (cache, *cache.rglob("*")):
        metadata = path.lstat()
        direct = not stat.S_ISLNK(metadata.st_mode) and path.resolve(strict=True) == path
        checks.require(direct, _check("cache_entries_direct"))
        expected_mode = 0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600
        checks.require(stat.S_IMODE(metadata.st_mode) == expected_mode, _check("cache_modes_exact"))
        checks.require(metadata.st_uid == os.geteuid(), _check("cache_owner_exact"))
        checks.require(not os.listxattr(path), _check("cache_xattrs_absent"))
        if stat.S_ISREG(metadata.st_mode):
            checks.require(metadata.st_nlink == 1, _check("cache_files_single_link"))
    return cache


def _validate_config(home: Path, cache: Path, checks: Checks) -> None:
    path = home / "config.toml"
    metadata = path.lstat()
    checks.require(stat.S_ISREG(metadata.st_mode), _check("config_regular"))
    checks.require(metadata.st_nlink == 1, _check("config_single_link"))
    checks.require(stat.S_IMODE(metadata.st_mode) == 0o600, _check("config_mode_exact"))
    parsed = _LOAD_TOML(path.read_text("utf-8"))
    features = parsed.get("features")
    marketplace = parsed.get("marketplaces")
    plugins = parsed.get("plugins")
    hooks = parsed.get("hooks")
    checks.require(
        isinstance(features, dict) and features.get("plugins") is True,
        _check("plugins_feature_enabled"),
    )
    local = marketplace.get(_MARKETPLACE) if isinstance(marketplace, dict) else None
    checks.require(
        isinstance(local, dict) and local.get("source_type") == "local",
        _check("marketplace_type_exact"),
    )
    checks.require(
        isinstance(local, dict) and local.get("source") == str(cache),
        _check("marketplace_source_exact"),
    )
    plugin = plugins.get(_PLUGIN_ID) if isinstance(plugins, dict) else None
    checks.require(
        isinstance(plugin, dict) and plugin.get("enabled") is True,
        _check("plugin_enabled"),
    )
    states = hooks.get("state") if isinstance(hooks, dict) else None
    prefix = f"{_PLUGIN_ID}:hooks/hooks.json:"
    owned = (
        {key: value for key, value in states.items() if key.startswith(prefix)}
        if isinstance(states, dict)
        else {}
    )
    checks.require(len(owned) == 1, _check("trusted_hook_count_exact"))
    for value in owned.values():
        trusted = value.get("trusted_hash") if isinstance(value, dict) else None
        checks.require(
            isinstance(value, dict) and value.get("enabled") is True,
            _check("trusted_hook_enabled"),
        )
        checks.require(
            isinstance(trusted, str) and trusted.startswith("sha256:") and len(trusted) == 71,
            _check("trusted_hook_hash_shape"),
        )
