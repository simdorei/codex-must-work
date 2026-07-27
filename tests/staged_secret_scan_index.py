from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from tests.staged_secret_scan_git import (
    GIT_ERROR,
    MAX_GIT_OUTPUT_BYTES,
    ScanError,
    run_git,
)
from tests.staged_secret_scan_snapshot import git_path

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

MALFORMED_GIT_OUTPUT: Final[str] = "MALFORMED_GIT_OUTPUT"
INTENT_TO_ADD: Final[str] = "INTENT_TO_ADD"
UNMERGED_INDEX: Final[str] = "UNMERGED_INDEX"
HEAD_UNREADABLE: Final[str] = "HEAD_UNREADABLE"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass(frozen=True, slots=True)
class StagedEntry:
    """A path from the index diff; deleted entries have no blob to inspect."""

    path: str
    deleted: bool


def _decode_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ScanError(MALFORMED_GIT_OUTPUT) from error
    if not path or "\x00" in path or path.startswith("/") or "\\" in path:
        raise ScanError(MALFORMED_GIT_OUTPUT)
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ScanError(MALFORMED_GIT_OUTPUT)
    return path


def parse_entries(raw: bytes) -> tuple[StagedEntry, ...]:
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        _ = fields.pop()
    entries: list[StagedEntry] = []
    index = 0
    while index < len(fields):
        try:
            status = fields[index].decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise ScanError(MALFORMED_GIT_OUTPUT) from error
        index += 1
        if not status or status[0] not in "ACDMRT":
            raise ScanError(MALFORMED_GIT_OUTPUT)
        if status[0] in "RC" and (len(status) != 4 or not status[1:].isdigit()):
            raise ScanError(MALFORMED_GIT_OUTPUT)
        if status[0] not in "RC" and status != status[0]:
            raise ScanError(MALFORMED_GIT_OUTPUT)
        path_count = 2 if status[0] in "RC" else 1
        if index + path_count > len(fields):
            raise ScanError(MALFORMED_GIT_OUTPUT)
        paths = tuple(_decode_path(fields[index + offset]) for offset in range(path_count))
        index += path_count
        for offset, path in enumerate(paths):
            entries.append(
                StagedEntry(path, status[0] == "D" or (status[0] == "R" and offset == 0))
            )
    return tuple(entries)


def _validate_stage_listing(raw: bytes) -> None:
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        _ = fields.pop()
    for field in fields:
        try:
            metadata, raw_path = field.split(b"\t", 1)
            mode, oid, stage = metadata.split()
        except ValueError as error:
            raise ScanError(MALFORMED_GIT_OUTPUT) from error
        path = _decode_path(raw_path)
        if stage != b"0":
            raise ScanError(UNMERGED_INDEX, path)
        if oid == b"0" * 40:
            raise ScanError(INTENT_TO_ADD, path)
        if mode not in {b"100644", b"100755", b"120000", b"160000"}:
            raise ScanError(MALFORMED_GIT_OUTPUT, path)


def _validate_debug_listing(debug: bytes) -> None:
    current_path: str | None = None
    for field in debug.split(b"\x00"):
        for line in field.splitlines():
            if not line.startswith(b" ") and b"\t" not in line:
                current_path = _decode_path(line)
                continue
            if b"flags:" not in line:
                continue
            if current_path is None:
                raise ScanError(MALFORMED_GIT_OUTPUT)
            try:
                flags = int(line.split(b"flags:", 1)[1].strip(), 16)
            except ValueError as error:
                raise ScanError(MALFORMED_GIT_OUTPUT, current_path) from error
            if flags & 0x20000000:
                raise ScanError(INTENT_TO_ADD, current_path)


def index_tree(root: Path, environment: Mapping[str, str]) -> str:
    _validate_stage_listing(run_git(root, "ls-files", "--stage", "-z", environment=environment))
    _validate_debug_listing(run_git(root, "ls-files", "--debug", "-z", environment=environment))
    try:
        return run_git(root, "write-tree", environment=environment).decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ScanError(MALFORMED_GIT_OUTPUT) from error


def _symbolic_head(root: Path, environment: Mapping[str, str]) -> str:
    try:
        raw_reference = run_git(
            root,
            "symbolic-ref",
            "-q",
            "HEAD",
            environment=environment,
        )
    except ScanError as error:
        if error.rule == GIT_ERROR:
            raise ScanError(HEAD_UNREADABLE) from error
        raise
    try:
        reference = raw_reference.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ScanError(HEAD_UNREADABLE) from error
    if not reference.startswith("refs/heads/") or "\n" in reference or "\r" in reference:
        raise ScanError(HEAD_UNREADABLE)
    try:
        _ = run_git(
            root,
            "check-ref-format",
            reference,
            environment=environment,
        )
    except ScanError as error:
        if error.rule == GIT_ERROR:
            raise ScanError(HEAD_UNREADABLE) from error
        raise
    return reference


def _loose_reference_exists(
    root: Path,
    reference: str,
    environment: Mapping[str, str],
) -> bool:
    reference_path = git_path(root, reference, environment)
    try:
        _ = reference_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ScanError(HEAD_UNREADABLE) from error
    return True


def _packed_reference_exists(
    root: Path,
    reference: str,
    environment: Mapping[str, str],
) -> bool:
    packed_refs = git_path(root, "packed-refs", environment)
    try:
        with packed_refs.open("rb") as source:
            total = 0
            for line in source:
                total += len(line)
                if total > MAX_GIT_OUTPUT_BYTES:
                    raise ScanError(HEAD_UNREADABLE)
                if line.startswith((b"#", b"^")):
                    continue
                try:
                    _oid, raw_name = line.rstrip(b"\r\n").split(b" ", 1)
                except ValueError as error:
                    raise ScanError(HEAD_UNREADABLE) from error
                if raw_name == reference.encode("ascii"):
                    return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ScanError(HEAD_UNREADABLE) from error
    return False


def _unborn_tree(root: Path, environment: Mapping[str, str]) -> str:
    reference = _symbolic_head(root, environment)
    try:
        _ = run_git(
            root,
            "show-ref",
            "--verify",
            "--quiet",
            reference,
            environment=environment,
        )
    except ScanError as error:
        if error.rule != GIT_ERROR:
            raise
        if _loose_reference_exists(root, reference, environment):
            raise ScanError(HEAD_UNREADABLE) from error
        if _packed_reference_exists(root, reference, environment):
            raise ScanError(HEAD_UNREADABLE) from error
        return EMPTY_TREE
    raise ScanError(HEAD_UNREADABLE)


def base_tree(root: Path, environment: Mapping[str, str]) -> str:
    try:
        raw_tree = run_git(
            root,
            "rev-parse",
            "--verify",
            "HEAD^{tree}",
            environment=environment,
        )
    except ScanError as error:
        if error.rule == GIT_ERROR:
            return _unborn_tree(root, environment)
        raise
    try:
        return raw_tree.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ScanError(MALFORMED_GIT_OUTPUT) from error


def tree_entries(
    root: Path,
    tree: str,
    environment: Mapping[str, str],
) -> dict[str, tuple[str, str, str]]:
    raw = run_git(root, "ls-tree", "-r", "-z", tree, environment=environment)
    fields = raw.split(b"\x00")
    if fields and fields[-1] == b"":
        _ = fields.pop()
    entries: dict[str, tuple[str, str, str]] = {}
    for field in fields:
        try:
            metadata, raw_path = field.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            path = _decode_path(raw_path)
            entries[path] = (mode.decode("ascii"), kind.decode("ascii"), oid.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ScanError(MALFORMED_GIT_OUTPUT) from error
    return entries
