"""Parse the exact platform hook commands bound into install receipts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, Protocol

from scripts.cache_security import read_source
from scripts.hook_trust import HookPlatform, read_plugin_manifest
from scripts.install_errors import InstallPluginError
from scripts.state_io import open_direct_file

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

_EVENTS: Final = (("UserPromptSubmit", "user_prompt_submit"),)
_PACKAGE_HOOKS_INVALID: Final = "package_hooks_invalid"

if TYPE_CHECKING:
    from pathlib import Path


class _JsonLoader(Protocol):
    def __call__(self, source: bytes, /) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class TrustedHookCommand:
    """Bind a persisted hook key to its exact selected command."""

    key: str
    command: str


def trusted_hook_commands_for_plugin(
    plugin_root: Path,
    marketplace_name: str,
    platform: HookPlatform | None = None,
) -> tuple[TrustedHookCommand, ...]:
    """Return commands in the same deterministic order as trusted hook states."""
    if (
        not marketplace_name
        or marketplace_name != marketplace_name.strip()
        or any(marker in marketplace_name for marker in ("@", ":"))
    ):
        _fail("marketplace_name_invalid")
    manifest = read_plugin_manifest(plugin_root)
    path = plugin_root / manifest.hook_manifest_path
    try:
        raw = _LOAD_JSON(read_source(path, _PACKAGE_HOOKS_INVALID, open_direct_file))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InstallPluginError(_PACKAGE_HOOKS_INVALID) from error
    if not isinstance(raw, dict) or set(raw) - {"description", "hooks"}:
        _fail("hook_manifest_invalid")
    hooks = raw.get("hooks")
    if not isinstance(hooks, dict) or set(hooks) != {event for event, _ in _EVENTS}:
        _fail("hook_handler_set_invalid")
    selected = platform or (HookPlatform.WINDOWS if os.name == "nt" else HookPlatform.POSIX)
    prefix = f"{manifest.name}@{marketplace_name}:{manifest.hook_manifest_path}"
    return tuple(
        TrustedHookCommand(
            f"{prefix}:{label}:0:0",
            _command(hooks[event], selected),
        )
        for event, label in _EVENTS
    )


def _command(value: JsonValue, platform: HookPlatform) -> str:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        _fail("hook_group_invalid")
    handlers = value[0].get("hooks")
    if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
        _fail("hook_handler_set_invalid")
    handler = handlers[0]
    command = handler.get("command")
    windows = handler.get("commandWindows")
    if not isinstance(command, str) or (windows is not None and not isinstance(windows, str)):
        _fail("hook_command_invalid")
    if platform is HookPlatform.WINDOWS:
        selected = command if windows is None else windows
    else:
        selected = command
    if not selected.strip():
        _fail("hook_command_invalid")
    return selected


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
