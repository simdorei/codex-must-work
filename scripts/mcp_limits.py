"""Bounded JSON parsing helpers for the MCP STDIO boundary."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from scripts.state_io import JsonValue

MAX_RAW_LINE_BYTES: Final = 1_048_576
MAX_NESTING_DEPTH: Final = 16
MAX_OBJECT_MEMBERS: Final = 128
MAX_ARRAY_ITEMS: Final = 128
MAX_STRING_BYTES: Final = 65_536


class DuplicateMemberError(ValueError):
    """Identify a JSON object containing a repeated member name."""


class MemberLimitError(ValueError):
    """Identify a JSON object exceeding the member limit."""


class MemberNameLimitError(ValueError):
    """Identify a JSON object member name exceeding the UTF-8 limit."""


class NonFiniteNumberError(ValueError):
    """Identify a non-finite JSON number token."""


class IntegerLimitError(ValueError):
    """Identify an integer exceeding the interpreter's safe digit limit."""


def object_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    """Build one JSON object while rejecting repeated or excessive members."""
    if len(pairs) > MAX_OBJECT_MEMBERS:
        raise MemberLimitError
    values: dict[str, JsonValue] = {}
    for key, value in pairs:
        try:
            key_bytes = len(key.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise MemberNameLimitError from error
        if key_bytes > MAX_STRING_BYTES:
            raise MemberNameLimitError
        if key in values:
            raise DuplicateMemberError
        values[key] = value
    return values


def reject_non_finite(value: str) -> JsonValue:
    """Reject JSON extensions such as NaN and Infinity."""
    raise NonFiniteNumberError(value)


def parse_int(value: str) -> int:
    """Convert a JSON integer while classifying interpreter digit-limit failures."""
    try:
        return int(value)
    except ValueError as error:
        raise IntegerLimitError from error


def validate_structure(root: JsonValue) -> None:
    """Enforce nesting, array, and decoded UTF-8 string limits."""
    stack: list[tuple[JsonValue, int, tuple[str | int, ...]]] = [(root, 1, ())]
    allow_auth_overlong = has_control_tool_shape(root)
    while stack:
        value, depth, path = stack.pop()
        if depth > MAX_NESTING_DEPTH:
            raise ValueError
        if type(value) is str:
            _validate_string(value, path, allow_auth_overlong)
        elif type(value) is dict:
            for key, child in value.items():
                stack.append((child, depth + 1, (*path, key)))
        elif type(value) is list:
            if len(value) > MAX_ARRAY_ITEMS:
                raise ValueError
            for index, child in enumerate(value):
                stack.append((child, depth + 1, (*path, index)))
        elif type(value) is float and not math.isfinite(value):
            raise ValueError


def _validate_string(value: str, path: tuple[str | int, ...], allow_auth_overlong: bool) -> None:
    try:
        string_bytes = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError from error
    if string_bytes > MAX_STRING_BYTES and not (
        allow_auth_overlong and path == ("params", "arguments", "session_id")
    ):
        raise ValueError


def has_control_tool_shape(root: JsonValue) -> bool:
    """Allow oversized auth fields to reach the auth-specific error boundary."""
    if type(root) is not dict or root.get("method") != "tools/call":
        return False
    params = root.get("params")
    if type(params) is not dict or params.get("name") not in {
        "cmw.work_on",
        "cmw.stop",
        "cmw.status",
        "cmw.complete",
        "cmw.settings",
        "cmw.notifications.setup",
    }:
        return False
    return type(params.get("arguments")) is dict
