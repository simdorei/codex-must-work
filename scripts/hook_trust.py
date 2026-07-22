"""Compute Codex-compatible trust identities for bundled hooks."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Never, Protocol, assert_never

from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from pathlib import Path

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_DEFAULT_HOOK_PATH: Final = "hooks/hooks.json"
_UINT64_MAX: Final = (1 << 64) - 1


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


class HookPlatform(StrEnum):
    """Select the platform command that Codex fingerprints."""

    WINDOWS = "windows"
    POSIX = "posix"


class _HookEvent(StrEnum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"

    @property
    def key_label(self) -> str:
        return _EVENT_LABELS[self]

    @property
    def supports_matcher(self) -> bool:
        return self not in {_HookEvent.USER_PROMPT_SUBMIT, _HookEvent.STOP}


_APPROVED_EVENTS: Final = tuple(_HookEvent)
_EVENT_LABELS: Final = {
    _HookEvent.SESSION_START: "session_start",
    _HookEvent.USER_PROMPT_SUBMIT: "user_prompt_submit",
    _HookEvent.STOP: "stop",
}
TRUSTED_HOOK_LABELS: Final = tuple(event.key_label for event in _APPROVED_EVENTS)
TRUSTED_HOOK_COUNT: Final = len(TRUSTED_HOOK_LABELS)


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Hold the trusted plugin metadata needed by installer stages."""

    name: str
    version: str
    hook_manifest_path: str


@dataclass(frozen=True, slots=True)
class TrustedHookState:
    """Represent one persisted Codex hook trust entry."""

    key: str
    trusted_hash: str


@dataclass(frozen=True, slots=True)
class _CommandHook:
    command: str
    command_windows: str | None
    timeout: int
    status_message: str | None


@dataclass(frozen=True, slots=True)
class _HookGroup:
    matcher: str | None
    handler: _CommandHook


def read_plugin_manifest(plugin_root: Path) -> PluginManifest:
    """Parse the plugin manifest and resolve its one approved hook declaration."""
    root = _resolved_plugin_root(plugin_root)
    raw = _read_json_object(_exact_source_file(root, ".codex-plugin/plugin.json"))
    name = _required_text(raw, "name", "plugin_manifest_name_invalid")
    version = _required_text(raw, "version", "plugin_manifest_version_invalid")
    hook_path = _DEFAULT_HOOK_PATH if "hooks" not in raw else _parse_hook_declaration(raw["hooks"])
    return PluginManifest(name=name, version=version, hook_manifest_path=hook_path)


def trusted_hook_states_for_plugin(
    plugin_root: Path,
    marketplace_name: str,
    platform: HookPlatform | None = None,
) -> tuple[TrustedHookState, ...]:
    """Return all lifecycle-hook trust states or fail without a partial result."""
    root = _resolved_plugin_root(plugin_root)
    manifest = read_plugin_manifest(root)
    marketplace = _strict_identifier(marketplace_name, "marketplace_name_invalid")
    hooks_path = _exact_source_file(root, manifest.hook_manifest_path)
    groups = _parse_hooks_file(hooks_path)
    selected_platform = platform or _current_platform()
    key_source = f"{manifest.name}@{marketplace}:{manifest.hook_manifest_path}"
    return tuple(
        TrustedHookState(
            key=f"{key_source}:{event.key_label}:0:0",
            trusted_hash=_command_hook_hash(event, groups[event], selected_platform),
        )
        for event in _APPROVED_EVENTS
    )


def _parse_hook_declaration(value: JsonValue) -> str:
    match value:
        case str() as path:
            pass
        case list() as paths:
            if len(paths) != 1:
                _fail("hook_manifest_count_invalid")
            path = paths[0]
            if not isinstance(path, str):
                _fail("hook_manifest_declaration_invalid")
        case _:
            assert_never(_fail("hook_manifest_declaration_invalid"))
    normalized = path.replace("\\", "/").removeprefix("./")
    if normalized != _DEFAULT_HOOK_PATH:
        _fail("hook_manifest_path_invalid", path)
    return normalized


def _parse_hooks_file(path: Path) -> dict[_HookEvent, _HookGroup]:
    raw = _read_json_object(path)
    if set(raw) - {"description", "hooks"}:
        _fail("hook_manifest_invalid", "unsupported top-level field")
    hooks = raw.get("hooks")
    if not isinstance(hooks, dict):
        _fail("hook_manifest_invalid", "hooks must be an object")
    expected = {event.value for event in _APPROVED_EVENTS}
    if set(hooks) != expected:
        _fail("hook_handler_set_invalid")
    return {event: _parse_group(event, hooks[event.value]) for event in _APPROVED_EVENTS}


def _parse_group(event: _HookEvent, value: JsonValue) -> _HookGroup:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        _fail("hook_group_invalid", event.value)
    group = value[0]
    if set(group) - {"matcher", "hooks"}:
        _fail("hook_group_invalid", event.value)
    matcher = group.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        _fail("hook_matcher_invalid", event.value)
    handlers = group.get("hooks")
    if not isinstance(handlers, list) or len(handlers) != 1:
        _fail("hook_handler_set_invalid", event.value)
    return _HookGroup(matcher=matcher, handler=_parse_handler(event, handlers[0]))


def _parse_handler(event: _HookEvent, value: JsonValue) -> _CommandHook:
    if not isinstance(value, dict):
        _fail("hook_handler_invalid", event.value)
    allowed = {"type", "command", "commandWindows", "timeout", "async", "statusMessage"}
    if set(value) - allowed or value.get("type") != "command":
        _fail("hook_handler_invalid", event.value)
    command = value.get("command")
    if not isinstance(command, str) or not command.strip():
        _fail("hook_command_invalid", event.value)
    command_windows = value.get("commandWindows")
    if command_windows is not None and not isinstance(command_windows, str):
        _fail("hook_command_invalid", event.value)
    asynchronous = value.get("async", False)
    if not isinstance(asynchronous, bool) or asynchronous:
        _fail("hook_async_invalid", event.value)
    status_message = value.get("statusMessage")
    if status_message is not None and not isinstance(status_message, str):
        _fail("hook_status_message_invalid", event.value)
    return _CommandHook(
        command=command,
        command_windows=command_windows,
        timeout=_normalized_timeout(event, value.get("timeout")),
        status_message=status_message,
    )


def _normalized_timeout(event: _HookEvent, value: JsonValue) -> int:
    if value is None:
        return 600
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _UINT64_MAX:
        _fail("hook_timeout_invalid", event.value)
    return max(value, 1)


def _command_hook_hash(event: _HookEvent, group: _HookGroup, platform: HookPlatform) -> str:
    windows_command = group.handler.command_windows
    commands = {
        HookPlatform.WINDOWS: group.handler.command if windows_command is None else windows_command,
        HookPlatform.POSIX: group.handler.command,
    }
    command = commands[platform]
    if not command.strip():
        _fail("hook_command_invalid", event.value)
    handler: JsonObject = {
        "type": "command",
        "command": command,
        "timeout": group.handler.timeout,
        "async": False,
    }
    if group.handler.status_message is not None:
        handler["statusMessage"] = group.handler.status_message
    identity: JsonObject = {"event_name": event.key_label, "hooks": [handler]}
    if event.supports_matcher and group.matcher is not None:
        identity["matcher"] = group.matcher
    canonical = json.dumps(_canonical_json(identity), ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _canonical_json(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_canonical_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_json(value[key]) for key in sorted(value)}
    return value


def _read_json_object(path: Path) -> JsonObject:
    try:
        parsed = _LOAD_JSON(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        reason = "invalid_json"
        raise InstallPluginError(reason, path.name) from exc
    if not isinstance(parsed, dict):
        _fail("invalid_json", f"{path.name} must contain an object")
    return parsed


def _resolved_plugin_root(plugin_root: Path) -> Path:
    try:
        root = plugin_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        reason = "plugin_root_invalid"
        raise InstallPluginError(reason) from exc
    if not root.is_dir():
        _fail("plugin_root_invalid")
    return root


def _exact_source_file(root: Path, relative_path: str) -> Path:
    expected = root.joinpath(*relative_path.split("/"))
    try:
        resolved = expected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        reason = "source_file_missing"
        raise InstallPluginError(reason, relative_path) from exc
    if resolved != expected or not resolved.is_file() or not resolved.is_relative_to(root):
        _fail("source_path_invalid", relative_path)
    return resolved


def _required_text(raw: JsonObject, key: str, reason_code: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise InstallPluginError(reason_code)
    return _strict_identifier(value, reason_code)


def _strict_identifier(value: str, reason_code: str) -> str:
    if not value or value != value.strip() or any(marker in value for marker in ("@", ":")):
        _fail(reason_code)
    return value


def _fail(reason_code: str, detail: str | None = None) -> Never:
    raise InstallPluginError(reason_code, detail)


def _current_platform() -> HookPlatform:
    return HookPlatform.WINDOWS if os.name == "nt" else HookPlatform.POSIX
