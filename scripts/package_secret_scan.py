"""Reject high-confidence secrets from the exact install package candidate."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Final, Never, cast

from scripts.cache_package import MANIFEST
from scripts.cache_security import read_source
from scripts.cache_semver import safe_relative
from scripts.install_errors import InstallPluginError
from scripts.state_io import open_direct_file

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

_MAX_FILES: Final = 4_096
_MAX_TEXT_FILE_BYTES: Final = 2 * 1024 * 1024
_MAX_SCANNED_BYTES: Final = 32 * 1024 * 1024
_SCAN_DOMAIN: Final = b"codex-must-work-secret-scan-v1\0"
_TEXT_SUFFIXES: Final = frozenset(
    {
        ".json",
        ".lock",
        ".md",
        ".env",
        ".ps1",
        ".py",
        ".rs",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_SECRET_PATTERNS: Final[tuple[re.Pattern[bytes], ...]] = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(rb"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(
        b"".join(
            (
                rb"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]{6,}/",
                rb"[A-Za-z0-9._-]{40,}",
            )
        )
    ),
)
_SECRET_DETECTED: Final = "package_candidate_secret_detected"  # noqa: S105
_SCAN_LIMIT: Final = "package_candidate_scan_limit_exceeded"


def scan_package_candidate(source_root: Path) -> str:
    """Scan bounded text members and return their manifest-bound seal."""
    manifest_data = _read_direct(source_root / MANIFEST, "package_source_unsafe")
    paths = _manifest_paths(manifest_data)
    if len(paths) > _MAX_FILES:
        _fail(_SCAN_LIMIT)
    seal = hashlib.sha256(_SCAN_DOMAIN + manifest_data)
    scanned = 0
    for relative in paths:
        if Path(relative).suffix.casefold() not in _TEXT_SUFFIXES:
            continue
        data = _read_direct(
            source_root.joinpath(*relative.split("/")),
            "package_source_unsafe",
        )
        if len(data) > _MAX_TEXT_FILE_BYTES:
            _fail(_SCAN_LIMIT)
        scanned += len(data)
        if scanned > _MAX_SCANNED_BYTES:
            _fail(_SCAN_LIMIT)
        if any(pattern.search(data) is not None for pattern in _SECRET_PATTERNS):
            _fail(_SECRET_DETECTED)
        encoded = relative.encode()
        seal.update(struct.pack(">I", len(encoded)))
        seal.update(encoded)
        seal.update(struct.pack(">Q", len(data)))
        seal.update(data)
    return seal.hexdigest()


def _manifest_paths(data: bytes) -> tuple[str, ...]:
    try:
        decoded = cast("JsonValue", json.loads(data))
    except (UnicodeError, json.JSONDecodeError):
        _fail("package_manifest_invalid")
    if not isinstance(decoded, list) or not all(isinstance(value, str) for value in decoded):
        _fail("package_manifest_invalid")
    paths = tuple(value for value in decoded if isinstance(value, str))
    valid = (
        paths
        and paths == tuple(sorted(paths, key=str.encode))
        and len(paths) == len(set(paths))
        and MANIFEST in paths
        and all(safe_relative(path) for path in paths)
    )
    if not valid:
        _fail("package_manifest_invalid")
    return paths


def _read_direct(path: Path, reason: str) -> bytes:
    return read_source(path, reason, open_direct_file)


def _fail(reason: str) -> Never:
    raise InstallPluginError(reason)
