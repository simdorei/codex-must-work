"""Edit only the installer-owned spans of Codex config.toml."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol

from scripts.config_publication import ConfigSnapshot, read_config_bytes, write_config_bytes
from scripts.hook_trust import TRUSTED_HOOK_LABELS
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import InstallerLease, installer_lock
from scripts.marketplace_identity import (
    LEGACY_MARKETPLACE_NAME,
    LEGACY_PLUGIN_ID,
    MARKETPLACE_NAME,
    MARKETPLACE_REF,
    MARKETPLACE_SOURCE,
    PLUGIN_ID,
)
from scripts.state_io import UnsafeStatePathError, ensure_existing_components_are_direct
from scripts.toml_lexical_editor import (
    UNSUPPORTED,
    Editor,
    TomlTable,
    TomlValue,
    drop,
    edit_boolean,
    edit_table,
    finish,
    table,
    value,
)

if TYPE_CHECKING:
    from scripts.hook_trust import TrustedHookState

__all__ = ("ConfigMutation", "TomlTable", "TomlValue", "render_config", "update_codex_config")

_PLUGIN: Final = PLUGIN_ID
_LEGACY: Final = LEGACY_PLUGIN_ID
_MARKETPLACE: Final = MARKETPLACE_NAME
_LEGACY_MARKETPLACE: Final = LEGACY_MARKETPLACE_NAME
_PREFIX: Final = f"{_PLUGIN}:hooks/hooks.json:"
_LEGACY_PREFIX: Final = f"{_LEGACY}:hooks/hooks.json:"
_EVENTS: Final = frozenset(TRUSTED_HOOK_LABELS)
_TARGET_ASSIGNMENT_PARTS: Final = (
    r"(?m)^\s*(?:features\s*\.\s*plugins|features\s*=\s*\{|",
    r"marketplaces\s*=\s*\{|plugins\s*=\s*\{|hooks\s*=\s*\{|",
    r"marketplaces\s*\.\s*(?:simdorei|codex-must-work-local)|",
    r"plugins\s*\.\s*[\"']codex-must-work@(?:codex-must-work-local|simdorei)[\"']|",
    r"hooks\s*\.\s*state\s*\.\s*[\"']codex-must-work@(?:codex-must-work-local|simdorei):)",
)
_TARGET_ASSIGNMENT: Final = re.compile("".join(_TARGET_ASSIGNMENT_PARTS))


class _TomlLoader(Protocol):
    def __call__(self, source: str, /) -> TomlTable: ...


_LOAD: Final[_TomlLoader] = tomllib.loads


@dataclass(frozen=True, slots=True)
class ConfigMutation:
    """Describe the complete desired installer-owned state."""

    source_root: Path
    trusted_hooks: tuple[TrustedHookState, ...]
    plugin_enabled: bool
    disable_legacy: bool = True


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)


def _parse(data: bytes, *, post_edit: bool = False) -> TomlTable:
    if data.startswith(b"\xef\xbb\xbf"):
        _fail("codex_config_bom")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("codex_config_malformed")
    without_crlf = text.replace("\r\n", "")
    if "\r" in without_crlf or ("\r\n" in text and "\n" in without_crlf):
        _fail(UNSUPPORTED)
    try:
        return _LOAD(text)
    except tomllib.TOMLDecodeError:
        _fail("codex_config_post_edit_invalid" if post_edit else "codex_config_malformed")


def _ensure(root: TomlTable, path: tuple[str, ...]) -> TomlTable:
    current = root
    for key in path:
        value = current.setdefault(key, {})
        if not isinstance(value, dict):
            _fail(UNSUPPORTED)
        current = value
    return current


def _apply_expected(expected: TomlTable, mutation: ConfigMutation) -> None:
    _ensure(expected, ("features",))["plugins"] = True
    _ensure(expected, ("marketplaces",))[_MARKETPLACE] = {
        "source_type": "git",
        "source": MARKETPLACE_SOURCE,
        "ref": MARKETPLACE_REF,
    }
    plugins = _ensure(expected, ("plugins",))
    plugins[_PLUGIN] = {"enabled": mutation.plugin_enabled}
    if mutation.disable_legacy:
        _ = plugins.pop(_LEGACY, None)
        _ = _ensure(expected, ("marketplaces",)).pop(_LEGACY_MARKETPLACE, None)
    state = _ensure(expected, ("hooks", "state"))
    for key in tuple(state):
        if key.startswith(_PREFIX) or (mutation.disable_legacy and key.startswith(_LEGACY_PREFIX)):
            del state[key]
    for hook in mutation.trusted_hooks:
        state[hook.key] = {"enabled": True, "trusted_hash": hook.trusted_hash}


def _validate_mutation(mutation: ConfigMutation) -> None:
    source = mutation.source_root
    try:
        ensure_existing_components_are_direct(Path(source.anchor), source)
        direct = source.is_absolute() and source.is_dir() and source.resolve(strict=True) == source
    except (OSError, RuntimeError, ValueError, UnsafeStatePathError):
        _fail("unsafe_source_root")
    if not direct:
        _fail("unsafe_source_root")
    expected = {f"{_PREFIX}{event}:0:0" for event in _EVENTS}
    keys = {hook.key for hook in mutation.trusted_hooks}
    valid_hashes = all(
        re.fullmatch(r"sha256:[0-9a-f]{64}", hook.trusted_hash) for hook in mutation.trusted_hooks
    )
    if len(mutation.trusted_hooks) != len(_EVENTS) or keys != expected or not valid_hashes:
        _fail("invalid_trusted_hook_state")


def _edit_features(editor: Editor, before: TomlTable) -> None:
    if (features := value(before, ("features",))) is None:
        edit_table(editor, ("features",), ("plugins = true",))
        return
    if not isinstance(features, dict):
        _fail(UNSUPPORTED)
    edit_boolean(editor, ("features", "plugins"), desired=True)


def _edit_owned_tables(
    editor: Editor,
    before: TomlTable,
    mutation: ConfigMutation,
) -> None:
    hooks = {hook.key: hook.trusted_hash for hook in mutation.trusted_hooks}
    state_value = value(before, ("hooks", "state"))
    if state_value is not None and not isinstance(state_value, dict):
        _fail(UNSUPPORTED)
    state = state_value if isinstance(state_value, dict) else {}
    for key in tuple(state):
        stale = key.startswith(_PREFIX) and key not in hooks
        legacy = mutation.disable_legacy and key.startswith(_LEGACY_PREFIX)
        if stale or legacy:
            drop(editor, ("hooks", "state", key))
    edit_table(
        editor,
        ("marketplaces", _MARKETPLACE),
        (
            'source_type = "git"',
            f"source = {json.dumps(MARKETPLACE_SOURCE)}",
            f"ref = {json.dumps(MARKETPLACE_REF)}",
        ),
    )
    edit_table(
        editor,
        ("plugins", _PLUGIN),
        (f"enabled = {str(mutation.plugin_enabled).lower()}",),
    )
    for key, trusted_hash in sorted(hooks.items()):
        edit_table(
            editor,
            ("hooks", "state", key),
            ("enabled = true", f"trusted_hash = {json.dumps(trusted_hash)}"),
        )
    if mutation.disable_legacy:
        _drop_legacy_tables(editor, before)


def _drop_legacy_tables(editor: Editor, before: TomlTable) -> None:
    for legacy in (
        ("plugins", _LEGACY),
        ("marketplaces", _LEGACY_MARKETPLACE),
    ):
        if value(before, legacy) is None:
            continue
        if table(before, legacy) is None:
            _fail(UNSUPPORTED)
        drop(editor, legacy)


def render_config(snapshot: ConfigSnapshot, mutation: ConfigMutation) -> bytes:
    """Render and prove the exact prior-state-derived semantic delta."""
    _validate_mutation(mutation)
    before = _parse(snapshot.data)
    if _TARGET_ASSIGNMENT.search(snapshot.data.decode("utf-8")):
        _fail(UNSUPPORTED)
    editor = Editor(snapshot, before)
    _edit_features(editor, before)
    _edit_owned_tables(editor, before, mutation)
    if snapshot.identity is None:
        notice = "[notice]\nhide_world_writable_warning = true\nhide_full_access_warning = true"
        editor.additions.insert(0, notice)
        before.update(_LOAD(notice))
    rendered = finish(editor)
    _apply_expected(before, mutation)
    if _parse(rendered, post_edit=True) != before:
        _fail("codex_config_post_edit_invalid")
    return rendered


def update_codex_config(
    codex_home: Path,
    mutation: ConfigMutation,
    lease: InstallerLease | None = None,
) -> bytes:
    """Apply one mutation under a caller-held or standalone outer lease."""
    if lease is not None:
        before = read_config_bytes(codex_home, lease)
        return write_config_bytes(lease, before, render_config(before, mutation))
    with installer_lock(codex_home) as acquired:
        before = read_config_bytes(codex_home, acquired)
        return write_config_bytes(acquired, before, render_config(before, mutation))
