"""Preserve TOML formatting while editing installer-owned table spans."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Final, Never, final

from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from scripts.config_publication import ConfigSnapshot

type TomlTable = dict[str, TomlValue]
type TomlValue = str | int | float | bool | datetime | date | time | list[TomlValue] | TomlTable

UNSUPPORTED: Final = "codex_config_unsupported_syntax"


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)


def table(root: TomlTable, path: tuple[str, ...]) -> TomlTable | None:
    """Return the nested table at path when every component is a table."""
    current = root
    for key in path:
        if not isinstance(value := current.get(key), dict):
            return None
        current = value
    return current


def value(root: TomlTable, path: tuple[str, ...]) -> TomlValue | None:
    """Return the parsed value at path, or None when its parent is absent."""
    return None if (parent := table(root, path[:-1])) is None else parent.get(path[-1])


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
            _fail(UNSUPPORTED)


@final
class Editor:
    """Collect byte-preserving edits for one parsed TOML snapshot."""

    def __init__(self, snapshot: ConfigSnapshot, tree: TomlTable) -> None:
        """Initialize lexical offsets and empty edit collections."""
        self.text = snapshot.data.decode("utf-8")
        self.tree = tree
        self.newline = "\r\n" if "\r\n" in self.text else "\n"
        suffix = self.text[len(self.text.rstrip("\r\n")) :]
        self.terminal = self.newline if snapshot.identity is None else suffix
        self.blocks = _blocks(self.text)
        self.edits: list[tuple[int, int, str]] = []
        self.additions: list[str] = []


def edit_table(editor: Editor, path: tuple[str, ...], lines: tuple[str, ...]) -> None:
    """Replace or append one simple table while preserving surrounding bytes."""
    header = _header(path)
    block = editor.blocks.get(header)
    replacement = editor.newline.join((header, *lines))
    if value(editor.tree, path) is None and block is None:
        editor.additions.append(replacement)
        return
    if not isinstance(value(editor.tree, path), dict) or block is None:
        _fail(UNSUPPORTED)
    _simple_block(editor.text, block)
    ending = editor.newline if editor.text[block[0] : block[2]].endswith(editor.newline) else ""
    editor.edits.append((block[0], block[2], replacement + ending))


def edit_boolean(editor: Editor, path: tuple[str, ...], *, desired: bool) -> None:
    """Replace one boolean assignment without changing its comment or spacing."""
    block = editor.blocks.get(_header(path[:-1]))
    if block is None:
        _fail(UNSUPPORTED)
    key = path[-1]
    body = editor.text[block[1] : block[2]]
    pattern = rf"(?m)^(?P<a>\s*{key}\s*=\s*)(?:true|false)(?P<b>\s*(?:#.*)?)(?=\r?$)"
    matches = list(re.finditer(pattern, body))
    if value(editor.tree, path) is None:
        editor.edits.append((block[1], block[1], f"{key} = {str(desired).lower()}{editor.newline}"))
        return
    if not isinstance(value(editor.tree, path), bool) or len(matches) != 1:
        _fail(UNSUPPORTED)
    match = matches[0]
    replacement = match.group("a") + str(desired).lower() + match.group("b")
    editor.edits.append((block[1] + match.start(), block[1] + match.end(), replacement))


def drop(editor: Editor, path: tuple[str, ...]) -> None:
    """Remove one simple table and its trailing blank lines."""
    if (block := editor.blocks.get(_header(path))) is None:
        _fail(UNSUPPORTED)
    _simple_block(editor.text, block)
    editor.edits.append((block[0], block[2], ""))


def finish(editor: Editor) -> bytes:
    """Apply collected edits in reverse offset order and return UTF-8 bytes."""
    text = editor.text
    for start, end, replacement in sorted(editor.edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if editor.additions:
        core = text.removesuffix(editor.terminal)
        separator = editor.newline * 2 if core else ""
        text = core + separator + (editor.newline * 2).join(editor.additions) + editor.terminal
    return text.encode("utf-8")
