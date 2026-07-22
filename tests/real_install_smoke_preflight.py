from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from scripts.cache_semver import higher
from tests.real_install_smoke_ledger import SmokeError, TrustEntry

if TYPE_CHECKING:
    from pathlib import Path

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type TomlValue = str | int | float | bool | list[TomlValue] | dict[str, TomlValue]
type TomlTable = dict[str, TomlValue]

_LAZY_NAME: Final = "lazy-eng-study-codex"
_CMW_PLUGIN: Final = "codex-must-work@codex-must-work-local"
_HOOK_PATH: Final = "hooks/hooks.json"


@dataclass(frozen=True, slots=True)
class HomePreflight:
    config_bytes: bytes
    lazy_plugin: str
    lazy_cache: Path
    lazy_hook_source: Path
    lazy_settings: Path
    effective_sources: tuple[Path, ...]


def inspect_home(
    home: Path,
    source_root: Path,
    managed_sources: tuple[Path, ...] = (),
) -> HomePreflight:
    config_path = home / "config.toml"
    config_data = _read_direct(config_path, "codex_config_unreadable")
    try:
        config = tomllib.loads(config_data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise SmokeError(reason := "codex_config_unreadable") from error
    plugins = _plugins(config)
    lazy_plugin = _lazy_plugin(plugins)
    lazy_cache = selected_cache(home, lazy_plugin)
    hook_source, expected = plugin_prompt_trust(lazy_cache, lazy_plugin)
    if _configured_trust(config, lazy_plugin) != expected:
        _fail("lazy_hook_not_exactly_trusted")
    settings = home / "plugins" / "data" / lazy_plugin.replace("@", "-")
    settings /= "lazy-eng-study-codex-settings.json"
    try:
        raw_settings = json.loads(_read_direct(settings, "lazy_settings_missing"))
    except (UnicodeError, json.JSONDecodeError) as error:
        reason = "lazy_settings_invalid"
        raise SmokeError(reason) from error
    if not isinstance(raw_settings, dict) or raw_settings.get("enabled") is not True:
        _fail("lazy_settings_not_enabled")
    sources = _prompt_sources(home, source_root, plugins, managed_sources)
    return HomePreflight(
        config_data,
        lazy_plugin,
        lazy_cache,
        hook_source,
        settings,
        tuple(sorted(path.resolve(strict=True) for path in sources)),
    )


def selected_cache(home: Path, plugin_id: str) -> Path:
    parts = plugin_id.split("@")
    if len(parts) != 2 or not all(parts):
        _fail("plugin_identity_invalid")
    root = home / "plugins" / "cache" / parts[1] / parts[0]
    try:
        candidates = [path for path in root.iterdir() if _direct_directory(path)]
    except OSError as error:
        reason = "selected_plugin_cache_missing"
        raise SmokeError(reason) from error
    if not candidates:
        _fail("selected_plugin_cache_missing")
    selected = candidates[0]
    for candidate in candidates[1:]:
        if higher(candidate.name, selected.name):
            selected = candidate
    return selected.resolve(strict=True)


def plugin_prompt_trust(cache: Path, plugin_id: str) -> tuple[Path, tuple[TrustEntry, ...]]:
    _, hooks_path = _plugin_files(cache)
    reason = "hook_source_unreadable"
    hooks = _json_object(_read_direct(hooks_path, reason), reason)
    events = hooks.get("hooks")
    groups = events.get("UserPromptSubmit") if isinstance(events, dict) else None
    if not isinstance(groups, list) or not groups:
        _fail("user_prompt_hook_missing")
    entries: list[TrustEntry] = []
    for group_index, group in enumerate(groups):
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list) or not handlers:
            _fail("user_prompt_hook_opaque")
        for handler_index, handler in enumerate(handlers):
            identity = _handler_identity(handler)
            key = f"{plugin_id}:{_HOOK_PATH}:user_prompt_submit:{group_index}:{handler_index}"
            canonical = json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            entries.append(TrustEntry(key, f"sha256:{digest}"))
    return hooks_path, tuple(entries)


def _plugin_files(cache: Path) -> tuple[Path, Path]:
    manifest_path = cache / ".codex-plugin" / "plugin.json"
    reason = "plugin_manifest_unreadable"
    manifest = _json_object(_read_direct(manifest_path, reason), reason)
    declaration = manifest.get("hooks", _HOOK_PATH)
    valid_list = isinstance(declaration, list) and declaration in [
        [_HOOK_PATH],
        [f"./{_HOOK_PATH}"],
    ]
    if declaration in (_HOOK_PATH, f"./{_HOOK_PATH}") or valid_list:
        relative = _HOOK_PATH
    else:
        _fail("plugin_hook_source_opaque")
    hooks = cache.joinpath(*relative.split("/"))
    _ = _read_direct(hooks, "hook_source_unreadable")
    return manifest_path.resolve(strict=True), hooks.resolve(strict=True)


def _configured_trust(config: TomlTable, plugin_id: str) -> tuple[TrustEntry, ...]:
    hooks = config.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    prefix = f"{plugin_id}:{_HOOK_PATH}:"
    values: list[TrustEntry] = []
    if isinstance(state, dict):
        for key, value in state.items():
            if key.startswith(prefix) and isinstance(value, dict) and value.get("enabled") is True:
                digest = value.get("trusted_hash")
                if isinstance(digest, str):
                    values.append(TrustEntry(key, digest))
    return tuple(sorted(values, key=lambda item: item.key))


def _handler_identity(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict) or value.get("type") != "command":
        _fail("user_prompt_hook_opaque")
    command = value.get("commandWindows") if os.name == "nt" else value.get("command")
    if command is None and os.name == "nt":
        command = value.get("command")
    timeout = value.get("timeout", 600)
    if not isinstance(command, str) or not command.strip() or not isinstance(timeout, int):
        _fail("user_prompt_hook_opaque")
    handler: JsonObject = {
        "type": "command",
        "command": command,
        "timeout": max(timeout, 1),
        "async": False,
    }
    status_message = value.get("statusMessage")
    if status_message is not None:
        if not isinstance(status_message, str):
            _fail("user_prompt_hook_opaque")
        handler["statusMessage"] = status_message
    return {"event_name": "user_prompt_submit", "hooks": [handler]}


def _has_prompt_hook(path: Path) -> bool:
    root = _json_object(_read_direct(path, "hook_source_unreadable"), "hook_source_unreadable")
    hooks = root.get("hooks")
    return isinstance(hooks, dict) and "UserPromptSubmit" in hooks


def _read_direct(path: Path, reason: str) -> bytes:
    try:
        named = path.lstat()
        if not stat.S_ISREG(named.st_mode) or stat.S_ISLNK(named.st_mode) or named.st_nlink != 1:
            raise SmokeError(reason)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read()
            opened = os.fstat(handle.fileno())
    except OSError as error:
        raise SmokeError(reason) from error
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        raise SmokeError(reason)
    return data


def _json_object(data: bytes, reason: str) -> JsonObject:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SmokeError(reason) from error
    if not isinstance(value, dict):
        raise SmokeError(reason)
    return value


def _direct_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and path.resolve(strict=True) == path
        )
    except (OSError, RuntimeError):
        return False


def _plugins(config: TomlTable) -> dict[str, TomlValue]:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        _fail("lazy_plugin_not_enabled")
    return plugins


def _lazy_plugin(plugins: dict[str, TomlValue]) -> str:
    lazy = [
        key
        for key, value in plugins.items()
        if key.split("@", 1)[0] == _LAZY_NAME
        and isinstance(value, dict)
        and value.get("enabled") is True
    ]
    if len(lazy) != 1:
        _fail("lazy_plugin_not_enabled")
    return lazy[0]


def _prompt_sources(
    home: Path,
    source_root: Path,
    plugins: dict[str, TomlValue],
    managed_sources: tuple[Path, ...],
) -> set[Path]:
    sources = set(managed_sources)
    for path in (home / "hooks.json", source_root / ".codex" / "hooks.json"):
        if path.exists() and _has_prompt_hook(path):
            sources.add(path.resolve(strict=True))
    for plugin_id, value in plugins.items():
        inactive = not isinstance(value, dict) or value.get("enabled") is not True
        if plugin_id == _CMW_PLUGIN or inactive:
            continue
        manifest, hook = _plugin_files(selected_cache(home, plugin_id))
        if _has_prompt_hook(hook):
            sources.update((manifest, hook))
    return sources


def _fail(reason: str) -> Never:
    raise SmokeError(reason)
