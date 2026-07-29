"""Bundled runtime launch and inactive-hook checks for the POSIX smoke."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from tests.native_posix_smoke_support import (
    CheckName,
    Checks,
    NativeLayout,
)
from tests.native_posix_tree_snapshot import tree_snapshot

if TYPE_CHECKING:
    from pathlib import Path

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

_EVENTS: Final = ("SessionStart", "UserPromptSubmit")
_SESSION_ID: Final = "native-smoke-session"
_AUDIT_SITE: Final = """import os
from pathlib import Path
import subprocess

Path(os.environ["CMW_NATIVE_SMOKE_AUDIT_LOADED"]).touch()

def blocked_popen(*args, **kwargs):
    Path(os.environ["CMW_NATIVE_SMOKE_CHILD_SENTINEL"]).touch()
    raise RuntimeError("child launch blocked by native smoke")

subprocess.Popen = blocked_popen
"""


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    layout: NativeLayout
    home: Path
    cache: Path


def _check(name: str) -> CheckName:
    return CheckName(name)


def extract_bundled_runtime(
    installed: InstalledPlugin,
    target: str,
    checks: Checks,
) -> Path:
    """Launch extraction and verify the installed interpreter's identity."""
    layout, home, cache = installed.layout, installed.home, installed.cache
    runtime_version, target_name = _runtime_details(layout, target, checks)
    python_version = runtime_version.split("+", maxsplit=1)[0]
    data = home / "plugins" / "data" / "codex-must-work-simdorei"
    env = layout.environment(home)
    env["PLUGIN_DATA"] = str(data)
    result = subprocess.run(  # noqa: S603
        (
            "/bin/sh",
            str(cache / "runtime" / "launch-python.sh"),
            "-c",
            "import platform;print(platform.python_version())",
        ),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=180,
        check=False,
    )
    checks.record_exit(result.returncode)
    checks.require(result.returncode == 0, _check("bundled_runtime_exit"))
    checks.require(
        result.stdout == f"{python_version}\n" and result.stderr == "",
        _check("bundled_runtime_version_exact"),
    )
    executable = (
        data / "portable-python" / runtime_version / target_name / "python" / "bin" / "python3"
    )
    metadata = executable.lstat()
    checks.require(
        stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1,
        _check("bundled_python_direct_file"),
    )
    checks.require(
        bool(stat.S_IMODE(metadata.st_mode) & 0o111),
        _check("bundled_python_executable"),
    )
    checks.require(
        not tuple(data.glob(".portable-python-stage.*")),
        _check("runtime_staging_clean"),
    )
    checks.require(not (data / ".portable-python.lock").exists(), _check("runtime_lock_clean"))
    return data


def run_inactive_hooks(installed: InstalledPlugin, data: Path, checks: Checks) -> int:
    """Exercise installed hooks with absent and explicitly disabled runtime state."""
    layout, home, cache = installed.layout, installed.home, installed.cache
    audit = layout.root / "audit"
    audit.mkdir(mode=0o700)
    _ = (audit / "sitecustomize.py").write_text(_AUDIT_SITE, encoding="utf-8")
    loaded = audit / "loaded"
    child = audit / "child"
    commands = _hook_commands(cache, checks)
    env = layout.environment(home)
    env.update(
        {
            "CMW_NATIVE_SMOKE_AUDIT_LOADED": str(loaded),
            "CMW_NATIVE_SMOKE_CHILD_SENTINEL": str(child),
            "PLUGIN_DATA": str(data),
            "PLUGIN_ROOT": str(cache),
            "PYTHONPATH": str(audit),
        }
    )

    def run_all() -> None:
        for event, command in commands:
            loaded.unlink(missing_ok=True)
            payload = json.dumps(
                {"session_id": _SESSION_ID, "turn_id": "turn-1", "hook_event_name": event}
            )
            result = subprocess.run(  # noqa: S603
                ("/bin/sh", "-c", command),
                env=env,
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
                check=False,
            )
            checks.record_exit(result.returncode)
            checks.require(result.returncode == 0, _check("inactive_hook_exit"))
            checks.require(
                result.stdout == "" and result.stderr == "",
                _check("inactive_hook_silent"),
            )
            checks.require(loaded.is_file(), _check("child_audit_loaded"))
            checks.require(not child.exists(), _check("inactive_hook_no_child"))

    state = home / "codex-must-work"
    checks.require(not state.exists(), _check("missing_runtime_initially_absent"))
    run_all()
    checks.require(not state.exists(), _check("missing_runtime_state_stable"))
    runtime = state / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    runtime.chmod(0o700)
    filename = hashlib.sha256(_SESSION_ID.encode()).hexdigest() + ".json"
    runtime_file = runtime / filename
    _ = runtime_file.write_bytes(b'{"schema_version":1,"enabled":false}\n')
    runtime_file.chmod(0o600)
    before = tree_snapshot(state)
    run_all()
    checks.require(tree_snapshot(state) == before, _check("disabled_runtime_state_stable"))
    checks.require(not child.exists(), _check("disabled_runtime_no_child"))
    return len(commands) * 2


def _runtime_details(layout: NativeLayout, target: str, checks: Checks) -> tuple[str, str]:
    manifest = _LOAD_JSON((layout.source_root / "runtime" / "manifest.json").read_text("utf-8"))
    checks.require(isinstance(manifest, dict), _check("runtime_manifest_object"))
    python_version = manifest.get("python") if isinstance(manifest, dict) else None
    release = manifest.get("release") if isinstance(manifest, dict) else None
    targets = manifest.get("targets") if isinstance(manifest, dict) else None
    selected = targets.get(target) if isinstance(targets, dict) else None
    archive = selected.get("archive") if isinstance(selected, dict) else None
    checks.require(isinstance(python_version, str), _check("runtime_python_version"))
    checks.require(isinstance(release, str), _check("runtime_release_present"))
    checks.require(isinstance(archive, str) and target in archive, _check("runtime_archive_target"))
    version = (
        f"{python_version}+{release}"
        if isinstance(python_version, str) and isinstance(release, str)
        else ""
    )
    return version, target


def _hook_commands(cache: Path, checks: Checks) -> tuple[tuple[str, str], ...]:
    parsed = _LOAD_JSON((cache / "hooks" / "hooks.json").read_text("utf-8"))
    hooks = parsed.get("hooks") if isinstance(parsed, dict) else None
    commands: list[tuple[str, str]] = []
    for event in _EVENTS:
        groups = hooks.get(event) if isinstance(hooks, dict) else None
        group = groups[0] if isinstance(groups, list) and len(groups) == 1 else None
        handlers = group.get("hooks") if isinstance(group, dict) else None
        handler = handlers[0] if isinstance(handlers, list) and len(handlers) == 1 else None
        command = handler.get("command") if isinstance(handler, dict) else None
        checks.require(isinstance(command, str), _check("cached_hook_command_present"))
        commands.append((event, command if isinstance(command, str) else ""))
    return tuple(commands)
