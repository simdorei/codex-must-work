"""Deterministically load and frame one cache package."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Final, Never, cast

from scripts.cache_semver import safe_relative
from scripts.cache_types import Package
from scripts.install_errors import InstallPluginError

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type DirectReader = Callable[[Path, str], bytes]

MANIFEST: Final = "runtime/package-files.json"
DOMAIN: Final = b"codex-must-work-package-v1\0"


def load_package(root: Path, reader: DirectReader) -> Package:
    """Read exactly the sorted manifest entries and compute their framed digest."""
    parsed = _json(reader(root / MANIFEST, "package_source_unsafe"), "package_manifest_invalid")
    if not isinstance(parsed, list) or not all(isinstance(value, str) for value in parsed):
        _fail("package_manifest_invalid")
    paths = tuple(value for value in parsed if isinstance(value, str))
    valid = (
        paths and paths == tuple(sorted(paths, key=str.encode)) and len(paths) == len(set(paths))
    )
    if not valid or MANIFEST not in paths or any(not safe_relative(path) for path in paths):
        _fail("package_manifest_invalid")
    files = tuple(
        (path, reader(root.joinpath(*path.split("/")), "package_source_unsafe")) for path in paths
    )
    values = dict(files)
    for required in (".codex-plugin/plugin.json", "hooks/hooks.json"):
        if not isinstance(_json(values.get(required, b""), "package_hooks_invalid"), dict):
            _fail("package_hooks_invalid")
    digest = hashlib.sha256(DOMAIN + struct.pack(">I", len(files)))
    for path, data in files:
        encoded = path.encode()
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
        digest.update(struct.pack(">Q", len(data)))
        digest.update(data)
    return Package(paths, files, digest.hexdigest())


def expected_directories(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return the exact directory closure implied by manifest files."""
    values: set[str] = set()
    for path in paths:
        parts = path.split("/")
        values.update("/".join(parts[:count]) for count in range(1, len(parts)))
    return tuple(sorted(values, key=str.encode))


def _json(data: bytes, reason: str) -> JsonValue:
    try:
        return cast("JsonValue", json.loads(data))
    except (UnicodeError, json.JSONDecodeError):
        _fail(reason)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
