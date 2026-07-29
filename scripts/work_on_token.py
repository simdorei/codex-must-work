"""Recognize only the documented explicit work-on invocation token."""

from __future__ import annotations

import unicodedata
from typing import Final

_WORK_ON_LITERAL: Final = "$work-on"
_SAFE_ASCII_WHITESPACE: Final = frozenset(" \t\r\n")


def contains_explicit_work_on(prompt: str) -> bool:
    """Return whether the raw prompt contains the exact literal invocation token."""
    offset = 0
    while (index := prompt.find(_WORK_ON_LITERAL, offset)) >= 0:
        before = prompt[index - 1] if index else None
        after_index = index + len(_WORK_ON_LITERAL)
        after = prompt[after_index] if after_index < len(prompt) else None
        if _is_valid_token_boundary(before) and _is_valid_token_boundary(after):
            return True
        offset = index + 1
    return False


def _is_valid_token_boundary(character: str | None) -> bool:
    if character is None or character in _SAFE_ASCII_WHITESPACE:
        return True
    category = unicodedata.category(character)
    if category in {"Zs", "Zl", "Zp"}:
        return True
    return category.startswith("P") and category != "Pc" and character != "-"
