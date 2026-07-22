"""Read exact installer-owned Codex config state under one lease."""

from __future__ import annotations

import re
import stat
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol

from scripts.config_metadata import ConfigSnapshot, read_config_bytes
from scripts.config_publication import write_config_bytes
from scripts.hook_trust import TrustedHookState, trusted_hook_states_for_plugin
from scripts.install_errors import InstallPluginError
from scripts.installer_cache_validation import (
    retained_cache_matches,
    snapshot_retained_cache,
    validate_cache_publication,
)

if TYPE_CHECKING:
    from scripts.cache_types import CacheIdentity, CachePublication
    from scripts.installer_lock import InstallerLease

type TomlTable = dict[str, TomlValue]
type TomlValue = str | int | float | bool | datetime | date | time | list[TomlValue] | TomlTable


class _TomlLoader(Protocol):
    def __call__(self, source: str, /) -> TomlTable: ...


def _toml_loader() -> _TomlLoader:
    return tomllib.loads


_LOAD_TOML: Final = _toml_loader()
_MALFORMED: Final = "codex_config_malformed"

_PLUGIN = "codex-must-work@codex-must-work-local"
_LEGACY = "codex-must-work@simdorei"
_MARKETPLACE = "codex-must-work-local"


@dataclass(frozen=True, slots=True)
class ConfigObservation:
    """Describe the last exact local-plugin config snapshot."""

    snapshot: ConfigSnapshot
    plugin_present: bool
    plugin_disabled: bool
    legacy_enabled: bool | None
    source_root: Path | None
    trusted_hooks: tuple[TrustedHookState, ...]


@dataclass(frozen=True, slots=True)
class PriorState:
    """Carry a prior snapshot and its qualified restore proof."""

    observation: ConfigObservation
    restorable_enabled: bool
    cache_identity: CacheIdentity | None
    cache_digest: str | None


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
    local = plugins.get(_PLUGIN)
    present = local is not None
    if present and not isinstance(local, dict):
        _fail("codex_config_unsupported_syntax")
    enabled = local.get("enabled") if isinstance(local, dict) else None
    if enabled is not None and not isinstance(enabled, bool):
        _fail("codex_config_unsupported_syntax")
    legacy = plugins.get(_LEGACY)
    if legacy is not None and not isinstance(legacy, dict):
        _fail("codex_config_unsupported_syntax")
    legacy_enabled = legacy.get("enabled") if isinstance(legacy, dict) else None
    if legacy_enabled is not None and not isinstance(legacy_enabled, bool):
        _fail("codex_config_unsupported_syntax")
    source = _marketplace_source(tree)
    hooks = _trusted_hooks(tree)
    return ConfigObservation(
        snapshot,
        present,
        enabled is not True,
        legacy_enabled if isinstance(legacy_enabled, bool) else None,
        source,
        hooks,
    )


def disable_local_plugin_only(codex_home: Path, lease: InstallerLease) -> ConfigObservation:
    """Compare-safely change only the canonical local plugin enabled token."""
    observed = observe_config(codex_home, lease)
    if observed.plugin_disabled:
        return observed
    text = observed.snapshot.data.decode("utf-8")
    header = re.compile(
        r'(?m)^\[plugins\."codex-must-work@codex-must-work-local"\][ \t]*(?:#.*)?\r?$'
    )
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


def classify_prior(codex_home: Path, lease: InstallerLease) -> PriorState:
    """Qualify an enabled prior cache for exact later restoration."""
    observed = observe_config(codex_home, lease)
    if observed.plugin_disabled or observed.source_root is None:
        return PriorState(
            observation=observed,
            restorable_enabled=False,
            cache_identity=None,
            cache_digest=None,
        )
    source = observed.source_root
    expected_parent = (
        codex_home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work"
    ).resolve(strict=False)
    try:
        named = source.lstat()
        direct = (
            stat.S_ISDIR(named.st_mode)
            and not stat.S_ISLNK(named.st_mode)
            and source.parent.resolve(strict=True) == expected_parent
            and source.resolve(strict=True) == source
        )
    except (OSError, RuntimeError):
        return PriorState(
            observation=observed,
            restorable_enabled=False,
            cache_identity=None,
            cache_digest=None,
        )
    if not direct:
        return PriorState(
            observation=observed,
            restorable_enabled=False,
            cache_identity=None,
            cache_digest=None,
        )
    try:
        expected = trusted_hook_states_for_plugin(source, _MARKETPLACE)
        restorable = observed.trusted_hooks == tuple(
            sorted(expected, key=lambda item: item.key)
        )
        identity, digest = snapshot_retained_cache(source) if restorable else (None, None)
    except (InstallPluginError, OSError):
        identity, digest, restorable = None, None, False
    return PriorState(
        observation=observed,
        restorable_enabled=restorable,
        cache_identity=identity,
        cache_digest=digest,
    )


def cache_matches_observation(
    observed: ConfigObservation,
    publication: CachePublication,
    trusted_hooks: tuple[TrustedHookState, ...],
    source_root: Path,
) -> bool:
    """Check enabled trust against one identity-bound publication."""
    if observed.plugin_disabled or observed.source_root != publication.cache_path:
        return False
    try:
        actual, digest = validate_cache_publication(publication, source_root)
    except (OSError, InstallPluginError):
        return False
    return (
        actual == publication.identity
        and digest == publication.digest
        and observed.trusted_hooks == tuple(sorted(trusted_hooks, key=lambda item: item.key))
    )


def prior_cache_still_valid(prior: PriorState) -> bool:
    """Revalidate the complete prior restore proof."""
    source = prior.observation.source_root
    expected = prior.cache_identity
    digest = prior.cache_digest
    if not prior.restorable_enabled or source is None or expected is None or digest is None:
        return False
    if not retained_cache_matches(source, expected, digest):
        return False
    try:
        trusted = trusted_hook_states_for_plugin(source, _MARKETPLACE)
    except InstallPluginError:
        return False
    return prior.observation.trusted_hooks == tuple(sorted(trusted, key=lambda item: item.key))


def observation_matches_prior(observed: ConfigObservation, prior: PriorState) -> bool:
    """Check whether final enabled state exactly matches the prior proof."""
    return (
        not observed.plugin_disabled
        and observed.source_root == prior.observation.source_root
        and observed.trusted_hooks == prior.observation.trusted_hooks
        and prior_cache_still_valid(prior)
    )


def _marketplace_source(tree: TomlTable) -> Path | None:
    marketplaces = tree.get("marketplaces")
    if not isinstance(marketplaces, dict):
        return None
    marketplace = marketplaces.get(_MARKETPLACE)
    if not isinstance(marketplace, dict) or marketplace.get("source_type") != "local":
        return None
    value = marketplace.get("source")
    if not isinstance(value, str):
        return None
    path = Path(value)
    return path if path.is_absolute() else None


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
