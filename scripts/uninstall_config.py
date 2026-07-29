"""Remove only byte-delimited CMW tables backed by validated install evidence."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Final, Never, Protocol

from scripts.install_errors import InstallPluginError
from scripts.marketplace_identity import (
    LEGACY_MARKETPLACE_NAME,
    LEGACY_PLUGIN_ID,
    MARKETPLACE_NAME,
    PLUGIN_ID,
)
from scripts.toml_lexical_headers import HeaderKind, TomlLexicalError, scan_table_headers

if TYPE_CHECKING:
    from scripts.config_publication import ConfigSnapshot
    from scripts.uninstall_evidence import ValidatedInstallEvidence

type TomlTable = dict[str, TomlValue]
type TomlValue = str | int | float | bool | datetime | date | time | list[TomlValue] | TomlTable
type TomlPath = tuple[str, ...]

_PREFIXES: Final = (
    f"{PLUGIN_ID}:hooks/hooks.json:",
    f"{LEGACY_PLUGIN_ID}:hooks/hooks.json:",
)
_OWNERSHIP_UNKNOWN: Final = "uninstall_config_ownership_unknown"
_UNSUPPORTED: Final = "codex_config_unsupported_syntax"


class _TomlLoader(Protocol):
    def __call__(self, source: str, /) -> TomlTable: ...


_LOAD: Final[_TomlLoader] = tomllib.loads


@dataclass(frozen=True, slots=True)
class _Block:
    header: str
    start: int
    body_start: int
    end: int
    kind: HeaderKind


def render_config_removal(
    snapshot: ConfigSnapshot,
    evidence: tuple[ValidatedInstallEvidence, ...],
) -> bytes:
    """Render a byte-preserving removal proven by exact cache-derived evidence."""
    data = snapshot.data
    if not data:
        return data
    tree, text = _parse(data)
    blocks = _blocks(text)
    _require_canonical_targets(tree, blocks)
    removals: list[tuple[int, int]] = []
    for block in blocks:
        path = _owned_path(block)
        if path is None:
            continue
        _require_owned_table(tree, path, evidence)
        if path[:1] == ("marketplaces",) and _marketplace_is_shared(tree, path[1]):
            continue
        removals.append((block.start, block.end))
    rendered = text
    for start, end in reversed(removals):
        rendered = rendered[:start] + rendered[end:]
    parsed, _ = _parse(rendered.encode("utf-8"), post_edit=True)
    _require_targets_absent(parsed)
    return rendered.encode("utf-8")


def has_uninstall_targets(snapshot: ConfigSnapshot) -> bool:
    """Return whether parsed config contains any CMW-owned target shape."""
    if not snapshot.data:
        return False
    tree, _ = _parse(snapshot.data)
    return bool(_target_paths(tree))


def _parse(data: bytes, *, post_edit: bool = False) -> tuple[TomlTable, str]:
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
        return _LOAD(text), text
    except tomllib.TOMLDecodeError:
        _fail("codex_config_post_edit_invalid" if post_edit else "codex_config_malformed")


def _blocks(text: str) -> tuple[_Block, ...]:
    try:
        headers = scan_table_headers(text)
    except TomlLexicalError:
        _fail(_UNSUPPORTED)
    result: list[_Block] = []
    for index, header in enumerate(headers):
        limit = headers[index + 1].start if index + 1 < len(headers) else len(text)
        body = text[header.body_start : limit]
        tail = re.search(r"(?m)(?:^[ \t]*(?:#.*)?(?:\r?\n|$))*\Z", body)
        end = header.body_start + (tail.start() if tail is not None else len(body))
        result.append(_Block(header.text, header.start, header.body_start, end, header.kind))
    return tuple(result)


def _owned_path(block: _Block) -> TomlPath | None:
    if block.kind is not HeaderKind.TABLE:
        return None
    exact = {
        f"[marketplaces.{MARKETPLACE_NAME}]": ("marketplaces", MARKETPLACE_NAME),
        f"[marketplaces.{LEGACY_MARKETPLACE_NAME}]": (
            "marketplaces",
            LEGACY_MARKETPLACE_NAME,
        ),
        f'[plugins."{PLUGIN_ID}"]': ("plugins", PLUGIN_ID),
        f'[plugins."{LEGACY_PLUGIN_ID}"]': ("plugins", LEGACY_PLUGIN_ID),
    }
    if block.header in exact:
        return exact[block.header]
    match = re.fullmatch(r'\[hooks\.state\."([^"]+)"\]', block.header)
    if match is not None and match.group(1).startswith(_PREFIXES):
        return "hooks", "state", match.group(1)
    return None


def _require_canonical_targets(tree: TomlTable, blocks: tuple[_Block, ...]) -> None:
    targets = _target_paths(tree)
    canonical = tuple(path for block in blocks if (path := _owned_path(block)) is not None)
    if len(canonical) != len(set(canonical)) or set(targets) != set(canonical):
        _fail(_UNSUPPORTED)


def _target_paths(tree: TomlTable) -> tuple[TomlPath, ...]:
    paths: list[TomlPath] = []
    marketplaces = tree.get("marketplaces")
    plugins = tree.get("plugins")
    hooks = tree.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    if isinstance(marketplaces, dict):
        paths.extend(
            ("marketplaces", name)
            for name in (MARKETPLACE_NAME, LEGACY_MARKETPLACE_NAME)
            if name in marketplaces
        )
    if isinstance(plugins, dict):
        paths.extend(("plugins", name) for name in (PLUGIN_ID, LEGACY_PLUGIN_ID) if name in plugins)
    if isinstance(state, dict):
        paths.extend(("hooks", "state", key) for key in state if key.startswith(_PREFIXES))
    return tuple(paths)


def _require_owned_table(
    tree: TomlTable,
    path: TomlPath,
    evidence: tuple[ValidatedInstallEvidence, ...],
) -> None:
    current = tree
    for key in path:
        value = current.get(key)
        if not isinstance(value, dict):
            _fail(_OWNERSHIP_UNKNOWN)
        current = value
    candidates = _evidence_for_path(path, evidence)
    if path[:1] == ("marketplaces",):
        if not any(current == _expected_marketplace(item) for item in candidates):
            _fail(_OWNERSHIP_UNKNOWN)
    elif path[:1] == ("plugins",):
        if set(current) != {"enabled"} or not isinstance(current.get("enabled"), bool):
            _fail(_OWNERSHIP_UNKNOWN)
    elif not any(
        current == {"enabled": True, "trusted_hash": hook.trusted_hash}
        for candidate in candidates
        for hook in candidate.trusted_hooks
        if hook.key == path[-1]
    ):
        _fail(_OWNERSHIP_UNKNOWN)


def _expected_marketplace(evidence: ValidatedInstallEvidence) -> TomlTable:
    expected: TomlTable = {
        "source_type": evidence.source_type,
        "source": evidence.source,
    }
    if evidence.reference is not None:
        expected["ref"] = evidence.reference
    return expected


def _evidence_for_path(
    path: TomlPath,
    evidence: tuple[ValidatedInstallEvidence, ...],
) -> tuple[ValidatedInstallEvidence, ...]:
    if path[:1] == ("marketplaces",):
        matches = tuple(item for item in evidence if item.marketplace_name == path[1])
    elif path[:1] == ("plugins",):
        matches = tuple(item for item in evidence if item.plugin_id == path[1])
    else:
        matches = tuple(
            item for item in evidence if any(hook.key == path[-1] for hook in item.trusted_hooks)
        )
    if not matches:
        _fail(_OWNERSHIP_UNKNOWN)
    return matches


def _require_targets_absent(tree: TomlTable) -> None:
    remaining = tuple(
        path
        for path in _target_paths(tree)
        if path[:1] != ("marketplaces",) or not _marketplace_is_shared(tree, path[1])
    )
    if remaining:
        _fail("codex_config_post_edit_invalid")


def _marketplace_is_shared(tree: TomlTable, marketplace: str) -> bool:
    plugins = tree.get("plugins")
    if not isinstance(plugins, dict):
        return False
    owned = {PLUGIN_ID, LEGACY_PLUGIN_ID}
    suffix = f"@{marketplace}"
    return any(key not in owned and key.endswith(suffix) for key in plugins)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
