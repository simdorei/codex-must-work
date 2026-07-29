"""Locate physical TOML table headers without entering strings or comments."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import Enum, auto
from typing import assert_never, final, override


@dataclass(frozen=True, slots=True)
class TomlHeader:
    """One complete physical header line and its byte-preserving offsets."""

    text: str
    start: int
    body_start: int
    kind: HeaderKind


class HeaderKind(Enum):
    """Distinguish ordinary tables from arrays of tables."""

    TABLE = auto()
    ARRAY_TABLE = auto()


class _State(Enum):
    NORMAL = auto()
    COMMENT = auto()
    BASIC = auto()
    LITERAL = auto()
    MULTILINE_BASIC = auto()
    MULTILINE_LITERAL = auto()


@dataclass(frozen=True, slots=True)
class TomlLexicalError(ValueError):
    """Identify the unterminated lexical state without exposing input text."""

    state: _State

    @override
    def __str__(self) -> str:
        """Return a public-safe lexical failure without source contents."""
        return f"unterminated TOML lexical state: {self.state.name.lower()}"


_DELIMITER_LENGTH = 3


@final
class _Scanner:
    """Accumulate lexical position and headers during one forward-only scan."""

    __slots__ = ("at_line_start", "headers", "line_start", "source", "state")

    def __init__(self, source: str) -> None:
        """Start one scanner at the beginning of an immutable source string."""
        self.source = source
        self.headers: list[TomlHeader] = []
        self.state = _State.NORMAL
        self.line_start = 0
        self.at_line_start = True

    def scan(self) -> tuple[TomlHeader, ...]:
        index = 0
        while index < len(self.source):
            index = self._step(index)
        if self.state not in {_State.NORMAL, _State.COMMENT}:
            raise TomlLexicalError(self.state)
        return tuple(self.headers)

    def _step(self, index: int) -> int:
        state = self.state
        match state:
            case _State.NORMAL:
                return self._normal(index)
            case _State.COMMENT:
                return self._comment(index)
            case _State.BASIC:
                return self._single(index, '"')
            case _State.LITERAL:
                return self._single(index, "'")
            case _State.MULTILINE_BASIC:
                return self._multiline(index, '"')
            case _State.MULTILINE_LITERAL:
                return self._multiline(index, "'")
            case _:
                assert_never(state)

    def _normal(self, index: int) -> int:
        character = self.source[index]
        if character == "\n":
            self._newline(index)
            return index + 1
        if self.at_line_start and character in " \t":
            return index + 1
        if self.at_line_start and character == "[":
            line_end = self.source.find("\n", index)
            body_start = len(self.source) if line_end < 0 else line_end + 1
            text = self.source[self.line_start : body_start].strip()
            kind = _header_kind(text)
            if kind is not None:
                self.headers.append(TomlHeader(text, self.line_start, body_start, kind))
        self.at_line_start = False
        if character == "#":
            self.state = _State.COMMENT
            return index + 1
        if character in "\"'":
            return self._open_string(index, character)
        return index + 1

    def _comment(self, index: int) -> int:
        if self.source[index] == "\n":
            self.state = _State.NORMAL
            self._newline(index)
        return index + 1

    def _single(self, index: int, quote: str) -> int:
        character = self.source[index]
        if self.state is _State.BASIC and character == "\\":
            return self._escape(index)
        if character == quote:
            self.state = _State.NORMAL
        elif character == "\n":
            raise TomlLexicalError(self.state)
        return index + 1

    def _multiline(self, index: int, quote: str) -> int:
        character = self.source[index]
        if self.state is _State.MULTILINE_BASIC and character == "\\":
            return self._escape(index)
        if character == quote:
            self.at_line_start = False
            run = _quote_run(self.source, index, quote)
            if run >= _DELIMITER_LENGTH:
                self.state = _State.NORMAL
            return index + run
        if character == "\n":
            self._newline(index)
        elif character not in " \t":
            self.at_line_start = False
        return index + 1

    def _open_string(self, index: int, quote: str) -> int:
        run = _quote_run(self.source, index, quote)
        multiline = run >= _DELIMITER_LENGTH
        if quote == '"':
            self.state = _State.MULTILINE_BASIC if multiline else _State.BASIC
        else:
            self.state = _State.MULTILINE_LITERAL if multiline else _State.LITERAL
        return index + (_DELIMITER_LENGTH if multiline else 1)

    def _escape(self, index: int) -> int:
        following = index + 1
        if following >= len(self.source):
            raise TomlLexicalError(self.state)
        if self.source[following] == "\n":
            self._newline(following)
        return following + 1

    def _newline(self, index: int) -> None:
        self.line_start = index + 1
        self.at_line_start = True


def scan_table_headers(source: str) -> tuple[TomlHeader, ...]:
    """Return table-like physical lines that occur in normal TOML lexical state."""
    return _Scanner(source).scan()


def _quote_run(source: str, index: int, quote: str) -> int:
    end = index
    while end < len(source) and source[end] == quote:
        end += 1
    return end - index


def _header_kind(text: str) -> HeaderKind | None:
    array_table = text.startswith("[[")
    opening = 2 if array_table else 1
    quote: str | None = None
    escaped = False
    index = opening
    while index < len(text):
        character = text[index]
        if quote is not None:
            if quote == '"' and character == "\\" and not escaped:
                escaped = True
            elif character == quote and not escaped:
                quote = None
            else:
                escaped = False
            index += 1
            continue
        if character in "\"'":
            quote = character
            index += 1
            continue
        closing = text.startswith("]]", index) if array_table else character == "]"
        if closing:
            width = 2 if array_table else 1
            suffix = text[index + width :].lstrip()
            if not suffix or suffix.startswith("#"):
                try:
                    _ = tomllib.loads(text)
                except tomllib.TOMLDecodeError:
                    return None
                return HeaderKind.ARRAY_TABLE if array_table else HeaderKind.TABLE
            return None
        index += 1
    return None
