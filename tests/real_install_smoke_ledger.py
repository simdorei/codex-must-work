from __future__ import annotations

import copy
import re
import stat
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, override

if TYPE_CHECKING:
    from pathlib import Path

type TomlValue = str | int | float | bool | list[TomlValue] | dict[str, TomlValue]
type TomlTable = dict[str, TomlValue]

CMW_EVENTS: Final = (
    "session_start",
    "user_prompt_submit",
    "stop",
)
_PLUGIN: Final = "codex-must-work@codex-must-work-local"
_MARKETPLACE: Final = "codex-must-work-local"
_PREFIX: Final = f"{_PLUGIN}:hooks/hooks.json:"
_LEGACY: Final = "codex-must-work@simdorei"
_HEADERS: Final = re.compile(rb"(?m)^\[([^\]\r\n]+)\][ \t]*(?:#.*)?\r?$")


class SmokeError(Exception):
    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class TrustEntry:
    key: str
    trusted_hash: str


@dataclass(frozen=True, slots=True)
class ConfigCheck:
    allowed_delta_exact: bool
    non_cmw_bytes_unchanged: bool
    trust_count: int


@dataclass(frozen=True, slots=True)
class FileState:
    name: str
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    data: bytes | None


@dataclass(frozen=True, slots=True)
class TreeState:
    records: tuple[FileState, ...]


def snapshot_tree(root: Path) -> TreeState:
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
    records = tuple(
        _state(path, "." if path == root else path.relative_to(root).as_posix()) for path in paths
    )
    return TreeState(records)


def require_same_tree(before: TreeState, after: TreeState) -> None:
    if before != after:
        _fail("second_install_wrote_state")


def snapshot_sources(paths: tuple[Path, ...]) -> TreeState:
    return TreeState(tuple(_state(path, str(path)) for path in sorted(paths)))


def require_same_sources(before: TreeState, after: TreeState) -> None:
    if before != after:
        _fail("effective_hook_sources_changed")


def verify_config_transition(
    before_data: bytes,
    after_data: bytes,
    selected_cache: Path,
    trusted: tuple[TrustEntry, ...],
) -> ConfigCheck:
    try:
        before = tomllib.loads(before_data.decode("utf-8"))
        after = tomllib.loads(after_data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        reason = "config_ledger_unreadable"
        raise SmokeError(reason) from error
    if _protected(before_data, before) != _protected(after_data, before):
        _fail("non_cmw_config_bytes_changed")
    if not _semantics(before, after, selected_cache, trusted):
        _fail("allowed_config_delta_mismatch")
    return ConfigCheck(
        allowed_delta_exact=True,
        non_cmw_bytes_unchanged=True,
        trust_count=len(trusted),
    )


def _state(path: Path, name: str) -> FileState:
    metadata = path.lstat()
    if stat.S_ISREG(metadata.st_mode):
        kind, data = "file", path.read_bytes()
    elif stat.S_ISDIR(metadata.st_mode):
        kind, data = "directory", None
    elif stat.S_ISLNK(metadata.st_mode):
        kind, data = "symlink", str(path.readlink()).encode()
    else:
        kind, data = "other", None
    return FileState(
        name,
        kind,
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        data,
    )


def _semantics(
    before: TomlTable,
    after: TomlTable,
    selected_cache: Path,
    trusted: tuple[TrustEntry, ...],
) -> bool:
    expected = {
        entry.key: {"enabled": True, "trusted_hash": entry.trusted_hash} for entry in trusted
    }
    features = after.get("features")
    markets = after.get("marketplaces")
    plugins = after.get("plugins")
    hooks = after.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    owned = (
        {key: value for key, value in state.items() if key.startswith(_PREFIX)}
        if isinstance(state, dict)
        else {}
    )
    exact = (
        len(trusted) == len(CMW_EVENTS)
        and {entry.key for entry in trusted} == {f"{_PREFIX}{event}:0:0" for event in CMW_EVENTS}
        and isinstance(features, dict)
        and features.get("plugins") is True
        and isinstance(markets, dict)
        and markets.get(_MARKETPLACE) == {"source_type": "local", "source": str(selected_cache)}
        and isinstance(plugins, dict)
        and plugins.get(_PLUGIN) == {"enabled": True}
        and owned == expected
    )
    if not exact:
        return False
    old, new = copy.deepcopy(before), copy.deepcopy(after)
    _remove(old, had_legacy=False)
    _remove(new, had_legacy=_has_legacy(before))
    _prune(old)
    _prune(new)
    return old == new


def _has_legacy(tree: TomlTable) -> bool:
    plugins = tree.get("plugins")
    return isinstance(plugins, dict) and isinstance(plugins.get(_LEGACY), dict)


def _remove(tree: TomlTable, *, had_legacy: bool) -> None:
    for parent, key in (("features", "plugins"), ("marketplaces", _MARKETPLACE)):
        table = tree.get(parent)
        if isinstance(table, dict):
            table.pop(key, None)
    plugins = tree.get("plugins")
    if isinstance(plugins, dict):
        plugins.pop(_PLUGIN, None)
        legacy = plugins.get(_LEGACY)
        if had_legacy and isinstance(legacy, dict):
            legacy.pop("enabled", None)
    hooks = tree.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    if isinstance(state, dict):
        for key in tuple(state):
            if key.startswith(_PREFIX):
                state.pop(key)


def _prune(tree: TomlTable) -> None:
    for key, value in tuple(tree.items()):
        if isinstance(value, dict):
            _prune(value)
            if not value:
                tree.pop(key)


def _protected(data: bytes, baseline: TomlTable) -> bytes:
    headers = list(_HEADERS.finditer(data))
    spans = _owned_table_spans(data, headers)
    feature = _assignment(data, headers, "features", b"plugins")
    if feature is not None:
        spans.append(feature)
    if _has_legacy(baseline):
        legacy = _assignment(data, headers, f'plugins."{_LEGACY}"', b"enabled")
        if legacy is not None:
            spans.append(legacy)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    output, cursor = bytearray(), 0
    for start, end in merged:
        output.extend(data[cursor:start])
        cursor = end
    output.extend(data[cursor:])
    return bytes(output)


def _owned_table_spans(data: bytes, headers: list[re.Match[bytes]]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    targets = {f"marketplaces.{_MARKETPLACE}", f'plugins."{_PLUGIN}"'}
    for index, header in enumerate(headers):
        name = header.group(1).decode("utf-8")
        if name in targets or name.startswith(f'hooks.state."{_PREFIX}'):
            start = header.start()
            if data[max(0, start - 4) : start] == b"\r\n\r\n":
                start -= 2
            elif data[max(0, start - 2) : start] == b"\n\n":
                start -= 1
            end = headers[index + 1].start() if index + 1 < len(headers) else len(data)
            spans.append((start, end))
    return spans


def _assignment(
    data: bytes, headers: list[re.Match[bytes]], table: str, key: bytes
) -> tuple[int, int] | None:
    for index, header in enumerate(headers):
        if header.group(1).decode("utf-8") == table:
            end = headers[index + 1].start() if index + 1 < len(headers) else len(data)
            matched = re.search(
                rb"(?m)^" + re.escape(key) + rb"[ \t]*=[ \t]*(true|false)",
                data[header.end() : end],
            )
            if matched is not None:
                return header.end() + matched.start(1), header.end() + matched.end(1)
    return None


def _fail(reason: str) -> Never:
    raise SmokeError(reason)
