"""Match the parsing and total ordering of Rust semver 1.0.27."""

from __future__ import annotations

import re
from typing import Final

_U64_MAX: Final = (1 << 64) - 1
_U64_TEXT: Final = str(_U64_MAX)
_CONTROL_CHARACTER_END: Final = 32
_CORE_PATTERN: Final = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_PRERELEASE_PATTERN: Final = r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
_BUILD_PATTERN: Final = r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
_SEMVER: Final = re.compile(
    f"{_CORE_PATTERN}{_PRERELEASE_PATTERN}{_BUILD_PATTERN}",
    re.ASCII,
)
type IdentifierKey = tuple[int, int, str, int]
type VersionKey = tuple[
    int,
    int,
    int,
    tuple[int, tuple[IdentifierKey, ...]],
    tuple[IdentifierKey, ...],
]


def safe_name(value: str) -> bool:
    """Reject path components unsafe on either supported host."""
    if (
        not value
        or value in {".", ".."}
        or any(
            ord(character) < _CONTROL_CHARACTER_END or character in '<>:"/\\|?*'
            for character in value
        )
        or value[-1] in {" ", "."}
    ):
        return False
    reserved = value.split(".", 1)[0].upper()
    return (
        reserved not in {"CON", "PRN", "AUX", "NUL"}
        and re.fullmatch(r"(?:COM|LPT)[1-9]", reserved, re.ASCII) is None
    )


def safe_relative(value: str) -> bool:
    """Accept one normalized slash-separated manifest path."""
    parts = value.split("/")
    return (
        "\\" not in value
        and "\0" not in value
        and all(part not in {"", ".", ".."} and safe_name(part) for part in parts)
    )


def version_key(value: str) -> VersionKey | None:
    """Parse a Rust-semver-compatible version into an orderable key."""
    matched = _SEMVER.fullmatch(value)
    if matched is None:
        return None
    core_text = tuple(matched[index] for index in range(1, 4))
    if any(
        len(component) > len(_U64_TEXT)
        or (len(component) == len(_U64_TEXT) and component > _U64_TEXT)
        for component in core_text
    ):
        return None
    core = tuple(int(component) for component in core_text)
    prerelease = tuple((matched[4] or "").split(".")) if matched[4] else ()
    if any(part.isdecimal() and len(part) > 1 and part[0] == "0" for part in prerelease):
        return None
    pre_key = (1, ()) if not prerelease else (0, tuple(_pre_key(part) for part in prerelease))
    build = tuple((matched[5] or "").split(".")) if matched[5] else ("",)
    return core[0], core[1], core[2], pre_key, tuple(_build_key(part) for part in build)


def higher(candidate: str, source: str) -> bool:
    """Use raw string order whenever either value is not valid semver."""
    candidate_key, source_key = version_key(candidate), version_key(source)
    if candidate_key is None or source_key is None:
        return candidate > source
    return candidate_key > source_key


def _pre_key(value: str) -> IdentifierKey:
    if value.isdecimal():
        return 0, len(value), value, 0
    return 1, 0, value, 0


def _build_key(value: str) -> IdentifierKey:
    if not value.isdecimal():
        return 1, 0, value, 0
    significant = value.lstrip("0")
    return 0, len(significant), significant, len(value)
