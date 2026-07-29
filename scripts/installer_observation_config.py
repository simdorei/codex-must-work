"""Observe and safely disable the canonical installer plugin configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Final, Never, Protocol

from scripts.config_metadata import ConfigSnapshot, read_config_bytes
from scripts.config_publication import write_config_bytes
from scripts.hook_trust import TrustedHookState
from scripts.install_errors import InstallPluginError
from scripts.installer_cache_observation import selected_cache_root
from scripts.marketplace_identity import (
    LEGACY_PLUGIN_ID,
    MARKETPLACE_NAME,
    MARKETPLACE_REF,
    MARKETPLACE_SOURCE,
    PLUGIN_ID,
)

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.installer_lock import InstallerLease

type TomlTable = dict[str, TomlValue]
type TomlValue = str | int | float | bool | datetime | date | time | list[TomlValue] | TomlTable


class _TomlLoader(Protocol):
    def __call__(self, source: str, /) -> TomlTable: ...


def _toml_loader() -> _TomlLoader:
    return tomllib.loads


_LOAD_TOML: Final = _toml_loader()
_MALFORMED: Final = "codex_config_malformed"

_PLUGIN = PLUGIN_ID
_LEGACY = LEGACY_PLUGIN_ID
_MARKETPLACE = MARKETPLACE_NAME


@dataclass(frozen=True, slots=True)
class ConfigObservation:
    """Describe the last exact simdorei-plugin config snapshot."""

    snapshot: ConfigSnapshot
    plugin_present: bool
    plugin_disabled: bool
    legacy_enabled: bool | None
    source_root: Path | None
    trusted_hooks: tuple[TrustedHookState, ...]


def observe_config(codex_home: Path, lease: InstallerLease) -> ConfigObservation:
    """Parse one exact config snapshot while retaining its identity."""
    snapshot = read_config_bytes(codex_home, lease)
    try:
        tree = _LOAD_TOML(snapshot.data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise InstallPluginError(_MALFORMED) from error
    plugins = tree.get("plugins", {})
    if not isinstance(plugins, dict):
        _fail("codex_config_unsupported_syntax")
    plugin = plugins.get(_PLUGIN)
    present = plugin is not None
    if present and not isinstance(plugin, dict):
        _fail("codex_config_unsupported_syntax")
    enabled = plugin.get("enabled") if isinstance(plugin, dict) else None
    if enabled is not None and not isinstance(enabled, bool):
        _fail("codex_config_unsupported_syntax")
    legacy = plugins.get(_LEGACY)
    if legacy is not None and not isinstance(legacy, dict):
        _fail("codex_config_unsupported_syntax")
    legacy_enabled = legacy.get("enabled") if isinstance(legacy, dict) else None
    if legacy_enabled is not None and not isinstance(legacy_enabled, bool):
        _fail("codex_config_unsupported_syntax")
    source = (
        selected_cache_root(codex_home)
        if present and _canonical_marketplace_configured(tree)
        else None
    )
    hooks = _trusted_hooks(tree)
    return ConfigObservation(
        snapshot,
        present,
        enabled is not True,
        legacy_enabled if isinstance(legacy_enabled, bool) else None,
        source,
        hooks,
    )


def disable_plugin_only(codex_home: Path, lease: InstallerLease) -> ConfigObservation:
    """Compare-safely change only the canonical simdorei plugin enabled token."""
    observed = observe_config(codex_home, lease)
    if observed.plugin_disabled:
        return observed
    text = observed.snapshot.data.decode("utf-8")
    header = re.compile(r'(?m)^\[plugins\."codex-must-work@simdorei"\][ \t]*(?:#.*)?\r?$')
    headers = list(header.finditer(text))
    if len(headers) != 1:
        _fail("codex_config_unsupported_syntax")
    start = headers[0].end()
    following = re.search(r"(?m)^\[\[?.*\]\]?[ \t]*(?:#.*)?\r?$", text[start:])
    end = len(text) if following is None else start + following.start()
    body = text[start:end]
    enabled = re.compile(
        r"(?m)^(?P<prefix>[ \t]*enabled[ \t]*=[ \t]*)true(?P<suffix>[ \t]*(?:#.*)?)(?=\r?$)"
    )
    matches = list(enabled.finditer(body))
    if len(matches) != 1:
        _fail("codex_config_unsupported_syntax")
    match = matches[0]
    replacement = match.group("prefix") + "false" + match.group("suffix")
    changed = text[: start + match.start()] + replacement + text[start + match.end() :]
    _ = write_config_bytes(lease, observed.snapshot, changed.encode("utf-8"))
    final = observe_config(codex_home, lease)
    if not final.plugin_disabled:
        _fail("plugin_disable_verification_failed")
    return final


def _canonical_marketplace_configured(tree: TomlTable) -> bool:
    marketplaces = tree.get("marketplaces")
    if not isinstance(marketplaces, dict):
        return False
    marketplace = marketplaces.get(_MARKETPLACE)
    return (
        isinstance(marketplace, dict)
        and marketplace.get("source_type") == "git"
        and marketplace.get("source") == MARKETPLACE_SOURCE
        and marketplace.get("ref", MARKETPLACE_REF) == MARKETPLACE_REF
    )


def _trusted_hooks(tree: TomlTable) -> tuple[TrustedHookState, ...]:
    hooks = tree.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    if not isinstance(state, dict):
        return ()
    prefix = f"{_PLUGIN}:hooks/hooks.json:"
    values: list[TrustedHookState] = []
    for key, raw in state.items():
        if not key.startswith(prefix):
            continue
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            continue
        digest = raw.get("trusted_hash")
        if isinstance(digest, str):
            values.append(TrustedHookState(key, digest))
    return tuple(sorted(values, key=lambda item: item.key))


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
