# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# How to run:
#   uv run python tests/staged_secret_scan.py --cached --redact

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.staged_secret_scan_git import (
    GIT_ERROR,
    GIT_OUTPUT_TOO_LARGE,
    ScanError,
    run_git,
)
from tests.staged_secret_scan_index import (
    HEAD_UNREADABLE,
    INTENT_TO_ADD,
    MALFORMED_GIT_OUTPUT,
    UNMERGED_INDEX,
    StagedEntry,
    base_tree,
    index_tree,
    parse_entries,
    tree_entries,
)
from tests.staged_secret_scan_patterns import (
    MAX_BLOB_BYTES,
    display_path,
    finding,
    has_control_path,
    is_allowed,
    is_forbidden_name,
    is_scannable_binary,
)
from tests.staged_secret_scan_snapshot import capture_snapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = (
    "HEAD_UNREADABLE",
    "INTENT_TO_ADD",
    "MALFORMED_GIT_OUTPUT",
    "UNMERGED_INDEX",
    "parse_entries",
    "run_git",
)

NOT_REPOSITORY: Final[str] = "NOT_REPOSITORY"
FORBIDDEN_PATH: Final[str] = "FORBIDDEN_PATH"
NON_ALLOWLISTED_PATH: Final[str] = "NON_ALLOWLISTED_PATH"
OVERSIZE_BLOB: Final[str] = "OVERSIZE_BLOB"
BINARY_BLOB: Final[str] = "BINARY_BLOB"
SYMLINK_ENTRY: Final[str] = "SYMLINK_ENTRY"
SUBMODULE_ENTRY: Final[str] = "SUBMODULE_ENTRY"
UNSAFE_PATH: Final[str] = "UNSAFE_PATH"


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Public scan counters, intentionally containing no candidate content."""

    staged_files: int
    scanned_blobs: int
    findings: int
    snapshot_id: str


def _canonical_path(path: Path) -> Path:
    """Resolve symlinks and normalize platform-specific path spelling."""
    return Path(os.path.normcase(os.path.realpath(path)))


def _repo_root(cwd: Path) -> Path:
    source_root = _canonical_path(cwd)
    discovery_environment = {"GIT_CEILING_DIRECTORIES": str(source_root.parent)}
    try:
        raw = run_git(
            source_root,
            "rev-parse",
            "--show-toplevel",
            environment=discovery_environment,
        )
    except ScanError as error:
        if error.rule == GIT_ERROR:
            raise ScanError(NOT_REPOSITORY) from error
        raise
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ScanError(MALFORMED_GIT_OUTPUT) from error
    if not text or "\n" in text or "\r" in text:
        raise ScanError(MALFORMED_GIT_OUTPUT)
    discovered_root = _canonical_path(Path(text))
    if discovered_root != source_root:
        raise ScanError(NOT_REPOSITORY)
    return source_root


def _scan_candidate(
    root: Path,
    snapshot_entries: dict[str, tuple[str, str, str]],
    entry: StagedEntry,
    environment: Mapping[str, str],
) -> None:
    tree_entry = snapshot_entries.get(entry.path)
    if tree_entry is None:
        raise ScanError(MALFORMED_GIT_OUTPUT, entry.path)
    mode, kind, oid = tree_entry
    if kind == "commit":
        raise ScanError(SUBMODULE_ENTRY, entry.path)
    if mode == "120000":
        raise ScanError(SYMLINK_ENTRY, entry.path)
    if kind != "blob":
        raise ScanError(MALFORMED_GIT_OUTPUT, entry.path)
    try:
        data = run_git(
            root,
            "cat-file",
            "blob",
            oid,
            environment=environment,
            max_stdout_bytes=MAX_BLOB_BYTES + 1,
        )
    except ScanError as error:
        if error.rule == GIT_OUTPUT_TOO_LARGE:
            raise ScanError(OVERSIZE_BLOB, entry.path) from error
        raise
    if len(data) > MAX_BLOB_BYTES:
        raise ScanError(OVERSIZE_BLOB, entry.path)
    if b"\x00" in data and not is_scannable_binary(entry.path):
        raise ScanError(BINARY_BLOB, entry.path)
    rule = finding(data)
    if rule is not None:
        raise ScanError(rule, entry.path)


def scan(cwd: Path) -> ScanResult:
    root = _repo_root(cwd)
    with capture_snapshot(root) as snapshot:
        tree = index_tree(root, snapshot.environment)
        base = base_tree(root, snapshot.environment)
        entries = parse_entries(
            run_git(
                root,
                "diff-tree",
                "-r",
                "--name-status",
                "-z",
                "--find-renames",
                "--no-commit-id",
                base,
                tree,
                environment=snapshot.environment,
            )
        )
        unique: dict[str, StagedEntry] = {entry.path: entry for entry in entries}
        snapshot_entries = tree_entries(root, tree, snapshot.environment)
        for entry in unique.values():
            if has_control_path(entry.path):
                raise ScanError(UNSAFE_PATH, entry.path)
            if is_forbidden_name(entry.path):
                raise ScanError(FORBIDDEN_PATH, entry.path)
            if not is_allowed(entry.path):
                raise ScanError(NON_ALLOWLISTED_PATH, entry.path)
        candidates = tuple(entry for entry in unique.values() if not entry.deleted)
        for entry in candidates:
            _scan_candidate(root, snapshot_entries, entry, snapshot.environment)
        return ScanResult(len(candidates), len(candidates), 0, snapshot.identifier)


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan staged allowlisted blobs for credentials.")
    _ = parser.add_argument("--cached", action="store_true", required=True)
    _ = parser.add_argument("--redact", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _ = _arguments(argv)
    try:
        result = scan(Path.cwd())
    except ScanError as error:
        print(f"{error.rule} {display_path(error.path)}")  # noqa: T201
        return 1
    message = (
        f"OK staged_files={result.staged_files} "
        f"scanned_blobs={result.scanned_blobs} findings={result.findings} "
        f"snapshot={result.snapshot_id}"
    )
    print(message)  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
