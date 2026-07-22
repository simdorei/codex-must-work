"""Edit only the installer-owned spans of Codex config.toml."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never, Protocol, final

from scripts.config_publication import ConfigSnapshot, read_config_bytes, write_config_bytes
from scripts.hook_trust import TRUSTED_HOOK_LABELS
from scripts.install_errors import InstallPluginError
from scripts.installer_lock import InstallerLease, installer_lock
from scripts.state_io import UnsafeStatePathError, ensure_existing_components_are_direct

if TYPE_CHECKING:
    from scripts.hook_trust import TrustedHookState

type TomlTable = dict[str, TomlValue]
type TomlValue = str | int | float | bool | datetime | date | time | list[TomlValue] | TomlTable
_PLUGIN: Final = "codex-must-work@codex-must-work-local"
_LEGACY: Final = "codex-must-work@simdorei"
_MARKETPLACE: Final = "codex-must-work-local"
_PREFIX: Final = f"{_PLUGIN}:hooks/hooks.json:"
_EVENTS: Final = frozenset(TRUSTED_HOOK_LABELS)
_UNSUPPORTED: Final = "codex_config_unsupported_syntax"
_TARGET_ASSIGNMENT_PARTS: Final = (
    r"(?m)^\s*(?:features\s*\.\s*plugins|features\s*=\s*\{|",
    r"marketplaces\s*=\s*\{|plugins\s*=\s*\{|hooks\s*=\s*\{|",
    r"marketplaces\s*\.\s*codex-must-work-local|",
    r"plugins\s*\.\s*[\"']codex-must-work@(?:codex-must-work-local|simdorei)[\"']|",
    r"hooks\s*\.\s*state\s*\.\s*[\"']codex-must-work@codex-must-work-local:)",
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
        _fail(_UNSUPPORTED)
    try:
        return _LOAD(text)
    except tomllib.TOMLDecodeError:
        _fail("codex_config_post_edit_invalid" if post_edit else "codex_config_malformed")


def _table(root: TomlTable, path: tuple[str, ...]) -> TomlTable | None:
    current = root
    for key in path:
        if not isinstance(value := current.get(key), dict):
            return None
        current = value
    return current


def _value(root: TomlTable, path: tuple[str, ...]) -> TomlValue | None:
    return None if (parent := _table(root, path[:-1])) is None else parent.get(path[-1])


def _header(path: tuple[str, ...]) -> str:
    parts = (
        part if re.fullmatch(r"[\w-]+", part) else json.dumps(part, ensure_ascii=False)
        for part in path
    )
    return f"[{'.'.join(parts)}]"


def _blocks(text: str) -> dict[str, tuple[int, int, int]]:
    headers = list(re.finditer(r"(?m)^[ \t]*\[\[?.*\]\]?[ \t]*(?:#.*)?(?:\r?\n|$)", text))
    result: dict[str, tuple[int, int, int]] = {}
    for index, header in enumerate(headers):
        limit = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end() : limit]
        tail = re.search(r"(?m)(?:^[ \t]*(?:#.*)?(?:\r?\n|$))*\Z", body)
        end = header.end() + (tail.start() if tail is not None else len(body))
        result[header.group().strip()] = header.start(), header.end(), end
    return result


def _simple_block(text: str, block: tuple[int, int, int]) -> None:
    for line in text[block[1] : block[2]].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*[\w-]+\s*=\s*(.*)", line)
        if match is None or match.group(1).lstrip().startswith(("[", "{", "'''", '"""')):
            _fail(_UNSUPPORTED)


@final
class _Editor:
    def __init__(self, snapshot: ConfigSnapshot, tree: TomlTable) -> None:
        self.text = snapshot.data.decode("utf-8")
        self.tree = tree
        self.newline = "\r\n" if "\r\n" in self.text else "\n"
        suffix = self.text[len(self.text.rstrip("\r\n")) :]
        self.terminal = self.newline if snapshot.identity is None else suffix
        self.blocks = _blocks(self.text)
        self.edits: list[tuple[int, int, str]] = []
        self.additions: list[str] = []


def _edit_table(editor: _Editor, path: tuple[str, ...], lines: tuple[str, ...]) -> None:
    header = _header(path)
    block = editor.blocks.get(header)
    replacement = editor.newline.join((header, *lines))
    if _value(editor.tree, path) is None and block is None:
        editor.additions.append(replacement)
        return
    if not isinstance(_value(editor.tree, path), dict) or block is None:
        _fail(_UNSUPPORTED)
    _simple_block(editor.text, block)
    ending = editor.newline if editor.text[block[0] : block[2]].endswith(editor.newline) else ""
    editor.edits.append((block[0], block[2], replacement + ending))


def _edit_boolean(editor: _Editor, path: tuple[str, ...], *, desired: bool) -> None:
    block = editor.blocks.get(_header(path[:-1]))
    if block is None:
        _fail(_UNSUPPORTED)
    key = path[-1]
    body = editor.text[block[1] : block[2]]
    pattern = rf"(?m)^(?P<a>\s*{key}\s*=\s*)(?:true|false)(?P<b>\s*(?:#.*)?)(?=\r?$)"
    matches = list(re.finditer(pattern, body))
    if _value(editor.tree, path) is None:
        editor.edits.append((block[1], block[1], f"{key} = {str(desired).lower()}{editor.newline}"))
        return
    if not isinstance(_value(editor.tree, path), bool) or len(matches) != 1:
        _fail(_UNSUPPORTED)
    match = matches[0]
    value = match.group("a") + str(desired).lower() + match.group("b")
    editor.edits.append((block[1] + match.start(), block[1] + match.end(), value))


def _drop(editor: _Editor, path: tuple[str, ...]) -> None:
    if (block := editor.blocks.get(_header(path))) is None:
        _fail(_UNSUPPORTED)
    _simple_block(editor.text, block)
    editor.edits.append((block[0], block[2], ""))


def _finish(editor: _Editor) -> bytes:
    text = editor.text
    for start, end, replacement in sorted(editor.edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if editor.additions:
        core = text.removesuffix(editor.terminal)
        separator = editor.newline * 2 if core else ""
        text = core + separator + (editor.newline * 2).join(editor.additions) + editor.terminal
    return text.encode("utf-8")


def _ensure(root: TomlTable, path: tuple[str, ...]) -> TomlTable:
    current = root
    for key in path:
        value = current.setdefault(key, {})
        if not isinstance(value, dict):
            _fail(_UNSUPPORTED)
        current = value
    return current


def _apply_expected(expected: TomlTable, mutation: ConfigMutation) -> None:
    _ensure(expected, ("features",))["plugins"] = True
    _ensure(expected, ("marketplaces",))[_MARKETPLACE] = {
        "source_type": "local",
        "source": str(mutation.source_root),
    }
    plugins = _ensure(expected, ("plugins",))
    plugins[_PLUGIN] = {"enabled": mutation.plugin_enabled}
    if mutation.disable_legacy and isinstance(legacy := plugins.get(_LEGACY), dict):
        legacy["enabled"] = False
    state = _ensure(expected, ("hooks", "state"))
    for key in tuple(state):
        if key.startswith(_PREFIX):
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


def _edit_features(editor: _Editor, before: TomlTable) -> None:
    if (features := _value(before, ("features",))) is None:
        _edit_table(editor, ("features",), ("plugins = true",))
        return
    if not isinstance(features, dict):
        _fail(_UNSUPPORTED)
    _edit_boolean(editor, ("features", "plugins"), desired=True)


def render_config(snapshot: ConfigSnapshot, mutation: ConfigMutation) -> bytes:
    """Render and prove the exact prior-state-derived semantic delta."""
    _validate_mutation(mutation)
    before = _parse(snapshot.data)
    if _TARGET_ASSIGNMENT.search(snapshot.data.decode("utf-8")):
        _fail(_UNSUPPORTED)
    editor = _Editor(snapshot, before)
    _edit_features(editor, before)
    hooks = {hook.key: hook.trusted_hash for hook in mutation.trusted_hooks}
    state_value = _value(before, ("hooks", "state"))
    if state_value is not None and not isinstance(state_value, dict):
        _fail(_UNSUPPORTED)
    state = state_value if isinstance(state_value, dict) else {}
    for key in tuple(state):
        if key.startswith(_PREFIX) and key not in hooks:
            _drop(editor, ("hooks", "state", key))
    _edit_table(
        editor,
        ("marketplaces", _MARKETPLACE),
        ('source_type = "local"', f"source = {json.dumps(str(mutation.source_root))}"),
    )
    _edit_table(
        editor,
        ("plugins", _PLUGIN),
        (f"enabled = {str(mutation.plugin_enabled).lower()}",),
    )
    for key, trusted_hash in sorted(hooks.items()):
        _edit_table(
            editor,
            ("hooks", "state", key),
            ("enabled = true", f"trusted_hash = {json.dumps(trusted_hash)}"),
        )
    legacy = ("plugins", _LEGACY)
    if mutation.disable_legacy and _value(before, legacy) is not None:
        if _table(before, legacy) is None:
            _fail(_UNSUPPORTED)
        _edit_boolean(editor, (*legacy, "enabled"), desired=False)
    if snapshot.identity is None:
        notice = "[notice]\nhide_world_writable_warning = true\nhide_full_access_warning = true"
        editor.additions.insert(0, notice)
        before.update(_LOAD(notice))
    rendered = _finish(editor)
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
