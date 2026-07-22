from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, NewType, final, override

CheckName = NewType("CheckName", str)


class RuntimeKind(StrEnum):
    DIRECT = "direct"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"


_REDIRECTED_KINDS: Final = frozenset((RuntimeKind.SYMLINK,))
_EXTRA_LINK_KINDS: Final = frozenset((RuntimeKind.HARDLINK,))


_FAKE_CODEX: Final = """#!/bin/sh
set -eu
marker=${CMW_NATIVE_SMOKE_DELAY_ONCE:-}
if [ -n "$marker" ]; then
    mkdir "$marker" 2>/dev/null || true
    sleep 3.8
fi
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
    printf '%s\\n' 'codex-cli 0.144.0'
    exit 0
fi
if [ "$#" -eq 2 ] && [ "$1" = "features" ] && [ "$2" = "list" ]; then
    plugins=false
    pattern='^[[:space:]]*plugins[[:space:]]*=[[:space:]]*true([[:space:]]|$)'
    if [ -f "${CODEX_HOME:?}/config.toml" ] && grep -Eq "$pattern" "${CODEX_HOME}/config.toml"; then
        plugins=true
    fi
    printf 'hooks stable true\\nplugins experimental %s\\n' "$plugins"
    exit 0
fi
if [ "$#" -eq 6 ] && [ "$1" = "plugin" ] && [ "$2" = "list" ] &&
    [ "$3" = "--available" ] && [ "$4" = "--json" ] &&
    [ "$5" = "--marketplace" ] && [ "$6" = "codex-must-work-local" ]; then
    marketplace='[{"name":"codex-must-work","marketplace":"codex-must-work-local",'
    marketplace=$marketplace'"source":{"source":"local","path":"./"}}]'
    printf '%s\\n' "$marketplace"
    exit 0
fi
printf '%s\\n' 'unexpected fake Codex command' >&2
exit 64
"""

_AUDIT_SITE: Final = """import os
from pathlib import Path
import subprocess

Path(os.environ["CMW_NATIVE_SMOKE_AUDIT_LOADED"]).touch()

def blocked_popen(*args, **kwargs):
    Path(os.environ["CMW_NATIVE_SMOKE_CHILD_SENTINEL"]).touch()
    raise RuntimeError("child launch blocked by native smoke")

subprocess.Popen = blocked_popen
"""

_TOOL_NAMES: Final = "awk basename chmod dirname grep mkdir mktemp mv rm sh sleep tar uname"
_TOOLS: Final[tuple[str, ...]] = tuple(_TOOL_NAMES.split())


@dataclass(frozen=True, slots=True)
class SmokeFailureError(RuntimeError):
    check: CheckName
    count: int
    last_exit: int

    @override
    def __str__(self) -> str:
        return str(self.check)


@final
class Checks:
    __slots__ = ("count", "last_exit")

    count: int
    last_exit: int

    def __init__(self) -> None:
        self.count = 0
        self.last_exit = 0

    def require(self, condition: bool, check: CheckName) -> None:
        self.count += 1
        if not condition:
            raise SmokeFailureError(check, self.count, self.last_exit)

    def record_exit(self, returncode: int) -> None:
        self.last_exit = returncode


@dataclass(frozen=True, slots=True)
class NativeLayout:
    source_root: Path
    root: Path
    temporary_root: Path
    command_bin: Path

    def environment(self, home: Path, delay_marker: Path | None = None) -> dict[str, str]:
        blocked_prefixes = ("CODEX_", "PLUGIN_", "PYTHON")
        env = {
            key: value for key, value in os.environ.items() if not key.startswith(blocked_prefixes)
        }
        env.update(
            {
                "CODEX_HOME": str(home),
                "HOME": str(self.root / "user-home"),
                "PATH": str(self.command_bin),
                "PYTHONNOUSERSITE": "1",
                "PYTHONUTF8": "1",
                "TMPDIR": str(self.temporary_root),
            }
        )
        if delay_marker is not None:
            env["CMW_NATIVE_SMOKE_DELAY_ONCE"] = str(delay_marker)
        return env


@dataclass(frozen=True, slots=True)
class TreeEntry:
    relative: str
    kind: Literal["directory", "file", "symlink"]
    mode: int
    device: int
    inode: int
    size: int
    modified_ns: int
    digest: str | None


def create_layout(source_root: Path) -> tuple[tempfile.TemporaryDirectory[str], NativeLayout]:
    system_temporary = Path(tempfile.gettempdir()).resolve(strict=True)
    allocation = tempfile.TemporaryDirectory(prefix="cmw-native-smoke.", dir=system_temporary)
    allocated = Path(allocation.name).absolute()
    named = allocated.lstat()
    root = allocated.resolve(strict=True)
    resolved = root.lstat()
    if (
        root.parent != system_temporary
        or stat.S_ISLNK(named.st_mode)
        or (named.st_dev, named.st_ino) != (resolved.st_dev, resolved.st_ino)
    ):
        allocation.cleanup()
        raise SmokeFailureError(CheckName("temporary_root_valid"), 1, 0)
    root.chmod(0o700)
    temporary_root = root / "tmp"
    command_bin = root / "command-bin"
    try:
        for directory in (temporary_root, command_bin, root / "user-home"):
            directory.mkdir(mode=0o700)
        _link_tools(command_bin)
    except (OSError, SmokeFailureError):
        allocation.cleanup()
        raise
    return allocation, NativeLayout(source_root, root, temporary_root, command_bin)


def create_home(layout: NativeLayout, name: str, kind: RuntimeKind = RuntimeKind.DIRECT) -> Path:
    home = layout.root / name
    sandbox = home / ".sandbox-bin"
    sandbox.mkdir(parents=True, mode=0o700)
    codex = sandbox / "codex"
    host = sandbox / "codex-code-mode-host"
    redirected = kind in _REDIRECTED_KINDS
    extra_link = kind in _EXTRA_LINK_KINDS
    if redirected:
        target = home / "unsafe-codex-target"
        _write_executable(target)
        codex.symlink_to(target)
    else:
        _write_executable(codex)
    _write_executable(host)
    if extra_link:
        os.link(codex, home / "unsafe-hardlink-alias")
    return home.resolve(strict=True)


def run_install(layout: NativeLayout, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ("/bin/sh", str(layout.source_root / "install.sh")),
        cwd=layout.root / "user-home",
        env=layout.environment(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=180,
        check=False,
    )


def start_install(
    layout: NativeLayout, home: Path, delay_marker: Path | None = None
) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        ("/bin/sh", str(layout.source_root / "install.sh")),
        cwd=layout.root / "user-home",
        env=layout.environment(home, delay_marker),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        _ = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        _ = process.wait(timeout=5)


def tree_snapshot(root: Path) -> tuple[TreeEntry, ...]:
    entries: list[TreeEntry] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: item.as_posix().encode()):
        metadata = path.lstat()
        kind: Literal["directory", "file", "symlink"]
        digest: str | None = None
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            kind = "symlink"
        entries.append(
            TreeEntry(
                "." if path == root else path.relative_to(root).as_posix(),
                kind,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                digest,
            )
        )
    return tuple(entries)


def bootstrap_clean(layout: NativeLayout) -> bool:
    names = (
        "cmw-installer-bootstrap.*",
        "cmw-marketplace-preflight-*",
        ".portable-python-stage.*",
    )
    return not any(True for pattern in names for _ in layout.temporary_root.glob(pattern))


def create_audit_site(directory: Path) -> None:
    _ = (directory / "sitecustomize.py").write_text(_AUDIT_SITE, encoding="utf-8")


def _write_executable(path: Path) -> None:
    _ = path.write_text(_FAKE_CODEX, encoding="utf-8", newline="\n")
    path.chmod(0o700)


def _link_tools(command_bin: Path) -> None:
    for name in _TOOLS:
        target = shutil.which(name)
        if target is None:
            raise SmokeFailureError(CheckName("native_toolset_complete"), 1, 0)
        _ = (command_bin / name).symlink_to(Path(target).resolve(strict=True))
    hashes = tuple(name for name in ("sha256sum", "shasum") if shutil.which(name) is not None)
    if not hashes:
        raise SmokeFailureError(CheckName("native_hash_tool_present"), 1, 0)
    for name in hashes:
        target = shutil.which(name)
        if target is not None:
            _ = (command_bin / name).symlink_to(Path(target).resolve(strict=True))
