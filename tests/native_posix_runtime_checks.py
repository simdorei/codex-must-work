"""Native POSIX checks for the installed direct MCP runtime and control key."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from tests.native_posix_smoke_support import CheckName

if TYPE_CHECKING:
    from pathlib import Path

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class CheckSink(Protocol):
    def require(self, condition: bool, check: CheckName) -> None: ...

    def record_exit(self, returncode: int) -> None: ...


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class RuntimeInspection:
    """Installed runtime roots bound to one native target."""

    source_root: Path
    cache: Path
    data: Path
    target: str


def _check(name: str) -> CheckName:
    return CheckName(name)


def validate_runtime_and_mcp(
    installed: RuntimeInspection,
    checks: CheckSink,
) -> None:
    """Verify installed runtime manifests, metadata, importability, and no bytecode."""
    source_root = installed.source_root
    cache = installed.cache
    data = installed.data
    target = installed.target
    manifest = _LOAD_JSON((source_root / "runtime" / "manifest.json").read_text("utf-8"))
    checks.require(isinstance(manifest, dict), _check("runtime_manifest_object"))
    python = manifest.get("python") if isinstance(manifest, dict) else None
    release = manifest.get("release") if isinstance(manifest, dict) else None
    targets = manifest.get("targets") if isinstance(manifest, dict) else None
    selected = targets.get(target) if isinstance(targets, dict) else None
    checks.require(
        isinstance(python, str) and isinstance(release, str) and isinstance(selected, dict),
        _check("runtime_manifest_target"),
    )
    version = f"{python}+{release}"
    runtime = data / f"portable-python-{version}" / "python"
    tree_name = selected.get("tree_manifest") if isinstance(selected, dict) else None
    exclusion_name = selected.get("bytecode_exclusion") if isinstance(selected, dict) else None
    exclusion_count = (
        selected.get("bytecode_exclusion_count") if isinstance(selected, dict) else None
    )
    exclusion_sha = (
        selected.get("bytecode_exclusion_sha256") if isinstance(selected, dict) else None
    )
    checks.require(
        isinstance(tree_name, str) and tree_name == f"{target}.json",
        _check("runtime_tree_manifest_named"),
    )
    checks.require(
        isinstance(exclusion_name, str) and exclusion_name == f"{target}.json",
        _check("runtime_exclusion_manifest_named"),
    )
    tree = _load_rows(source_root / "runtime" / "manifests" / str(tree_name), checks)
    exclusions_path = source_root / "runtime" / "exclusions" / str(exclusion_name)
    exclusions_bytes = exclusions_path.read_bytes()
    exclusions = _load_rows(exclusions_path, checks)
    checks.require(
        isinstance(exclusion_count, int)
        and not isinstance(exclusion_count, bool)
        and exclusion_count == len(exclusions),
        _check("runtime_exclusion_count_exact"),
    )
    checks.require(
        isinstance(exclusion_sha, str)
        and hashlib.sha256(exclusions_bytes).hexdigest() == exclusion_sha,
        _check("runtime_exclusion_hash_exact"),
    )
    checks.require(
        all(
            isinstance(row.get("path"), str) and str(row.get("path")).endswith(".pyc")
            for row in exclusions
        ),
        _check("runtime_exclusions_are_bytecode"),
    )
    _validate_tree(runtime, tree, checks)
    _validate_control_key(data, checks)
    _require_no_bytecode(data, checks)
    result = subprocess.run(  # noqa: S603
        (
            str(runtime / "python"),
            "-B",
            "-c",
            "import scripts.mcp_server;print('mcp_import=ok')",
        ),
        cwd=cache,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PLUGIN_DATA": str(data),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
        check=False,
    )
    checks.record_exit(result.returncode)
    checks.require(result.returncode == 0, _check("mcp_import_exit"))
    checks.require(
        result.stdout == "mcp_import=ok\n" and result.stderr == "",
        _check("mcp_import_output_exact"),
    )
    _require_no_bytecode(data, checks)


def require_no_bytecode(data: Path, checks: CheckSink) -> None:
    """Require the installed private data tree to contain no Python bytecode."""
    _require_no_bytecode(data, checks)


def _load_rows(path: Path, checks: CheckSink) -> tuple[dict[str, JsonValue], ...]:
    parsed = _LOAD_JSON(path.read_text("utf-8"))
    checks.require(isinstance(parsed, list), _check("runtime_manifest_array"))
    rows = tuple(row for row in parsed if isinstance(row, dict)) if isinstance(parsed, list) else ()
    checks.require(
        isinstance(parsed, list) and len(rows) == len(parsed),
        _check("runtime_manifest_rows"),
    )
    return rows


def _validate_tree(
    runtime: Path,
    rows: tuple[dict[str, JsonValue], ...],
    checks: CheckSink,
) -> None:
    expected = tuple(str(row.get("path")) for row in rows)
    actual = tuple(
        sorted(
            (path.relative_to(runtime).as_posix() for path in runtime.rglob("*")),
            key=str.encode,
        )
    )
    checks.require(actual == expected, _check("runtime_tree_exact"))
    for row in rows:
        relative = row.get("path")
        kind = row.get("type")
        executable = row.get("executable")
        size = row.get("size")
        digest = row.get("sha256")
        checks.require(
            isinstance(relative, str)
            and kind in {"directory", "file"}
            and isinstance(executable, bool),
            _check("runtime_manifest_entry_shape"),
        )
        path = runtime.joinpath(*str(relative).split("/"))
        metadata = path.lstat()
        directory = kind == "directory"
        direct = not stat.S_ISLNK(metadata.st_mode) and path.resolve(strict=True) == path
        checks.require(direct and metadata.st_nlink == 1, _check("runtime_entry_direct"))
        expected_mode = 0o700 if directory or executable else 0o600
        checks.require(
            stat.S_IMODE(metadata.st_mode) == expected_mode,
            _check("runtime_entry_mode_exact"),
        )
        checks.require(metadata.st_uid == os.geteuid(), _check("runtime_entry_owner_exact"))
        if not directory:
            data = path.read_bytes()
            checks.require(
                isinstance(size, int)
                and not isinstance(size, bool)
                and len(data) == size
                and isinstance(digest, str)
                and hashlib.sha256(data).hexdigest() == digest,
                _check("runtime_entry_content_exact"),
            )
    wrapper = runtime / "python"
    checks.require(
        stat.S_IMODE(wrapper.lstat().st_mode) == 0o700,
        _check("runtime_wrapper_executable_mode"),
    )


def _validate_control_key(data: Path, checks: CheckSink) -> None:
    key = data / "control.key"
    metadata = key.lstat()
    checks.require(
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_size == 32
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_uid == os.geteuid()
        and key.resolve(strict=True) == key,
        _check("control_key_metadata_exact"),
    )


def _require_no_bytecode(data: Path, checks: CheckSink) -> None:
    checks.require(
        not any(path.suffix == ".pyc" for path in data.rglob("*")),
        _check("installed_bytecode_absent"),
    )
