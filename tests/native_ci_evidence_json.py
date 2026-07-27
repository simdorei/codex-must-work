"""Resource-bounded strict JSON decoding for native CI metadata."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING, Final, Never, Protocol, final

if TYPE_CHECKING:
    from collections.abc import Iterator

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

MAX_JSON_NESTING: Final = 64
MAX_JSON_NUMBER_DIGITS: Final = 128
_DIGITS: Final = frozenset("0123456789")
_NUMBER_CONTINUATIONS: Final = frozenset(".eE+-")


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


@final
class JsonInputError(ValueError):
    """Signal malformed or over-budget untrusted JSON without retaining input."""


@final
class _DuplicateMemberError(ValueError):
    """Signal a duplicate JSON object member at any nesting depth."""


@final
class _NonStandardConstantError(ValueError):
    """Signal a Python-only numeric constant in untrusted JSON."""


@final
class _JsonResourceError(ValueError):
    """Signal a JSON nesting or numeric-token resource excess."""


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMemberError
        result[key] = value
    return result


def _reject_constant(_source: str) -> Never:
    raise _NonStandardConstantError


def _json_loader() -> _JsonLoader:
    return partial(
        json.loads,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


_LOAD_JSON: Final = _json_loader()


def load_json(source: str) -> JsonValue:
    """Decode strict JSON after a linear resource-budget scan."""
    try:
        _enforce_resource_limits(source)
        return _LOAD_JSON(source)
    except (UnicodeDecodeError, RecursionError, ValueError):
        raise JsonInputError from None


def _enforce_resource_limits(source: str) -> None:
    _enforce_nesting(source)
    _enforce_number_digits(source)


def _outside_string_characters(source: str) -> Iterator[str]:
    inside_string = False
    escaped = False
    for character in source:
        if not inside_string:
            if character == '"':
                inside_string = True
            else:
                yield character
        elif escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            inside_string = False


def _enforce_nesting(source: str) -> None:
    depth = 0
    for character in _outside_string_characters(source):
        if character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise _JsonResourceError
        elif character in "]}":
            depth -= 1


def _enforce_number_digits(source: str) -> None:
    number_digits = 0
    for character in _outside_string_characters(source):
        if character in _DIGITS:
            number_digits += 1
            if number_digits > MAX_JSON_NUMBER_DIGITS:
                raise _JsonResourceError
        elif number_digits > 0 and character in _NUMBER_CONTINUATIONS:
            continue
        else:
            number_digits = 0
