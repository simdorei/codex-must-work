# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run tests/native_posix_install_smoke.py
"""# noqa: SIZE_OK — one candidate-bound native E2E with one allowed helper."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as datetime_time
from pathlib import Path
from typing import Final, Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.native_posix_smoke_support import (
    CheckName,
    Checks,
    NativeLayout,
    RuntimeKind,
    SmokeFailureError,
    bootstrap_clean,
    create_audit_site,
    create_home,
    create_layout,
    run_install,
    start_install,
    stop_process,
    tree_snapshot,
)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type TomlTable = dict[str, TomlValue]
type TomlValue = (
    str | int | float | bool | datetime | date | datetime_time | list[TomlValue] | TomlTable
)


class _JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


class _TomlLoader(Protocol):
    def __call__(self, source: str, /) -> TomlTable: ...


def _json_loader() -> _JsonLoader:
    return json.loads


def _toml_loader() -> _TomlLoader:
    return tomllib.loads


_LOAD_JSON: Final = _json_loader()
_LOAD_TOML: Final = _toml_loader()
_EVENTS: Final = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
)
_PLUGIN_ID: Final = "codex-must-work@codex-must-work-local"
_MARKETPLACE: Final = "codex-must-work-local"
_SESSION_ID: Final = "native-smoke-session"


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    layout: NativeLayout
    home: Path
    cache: Path


def _check(name: str) -> CheckName:
    return CheckName(name)


def _native_target(checks: Checks) -> str:
    targets = {
        ("Linux", "x86_64"): "linux-x64",
        ("Darwin", "arm64"): "macos-arm64",
    }
    selected = targets.get((platform.system(), platform.machine()))
    checks.require(selected is not None, _check("native_host_supported"))
    return selected or ""


def _unsafe_runtime_case(layout: NativeLayout, kind: RuntimeKind, checks: Checks) -> None:
    home = create_home(layout, f"{kind}-home", kind)
    before = tree_snapshot(home)
    result = run_install(layout, home)
    checks.record_exit(result.returncode)
    checks.require(result.returncode != 0, _check(f"{kind}_rejected"))
    checks.require("install=ok" not in result.stdout, _check(f"{kind}_success_absent"))
    checks.require(tree_snapshot(home) == before, _check(f"{kind}_home_stable"))
    checks.require(not (home / "config.toml").exists(), _check(f"{kind}_config_absent"))
    checks.require(bootstrap_clean(layout), _check(f"{kind}_bootstrap_clean"))


def _manifest_version(source_root: Path, checks: Checks) -> str:
    parsed = _LOAD_JSON((source_root / ".codex-plugin" / "plugin.json").read_text("utf-8"))
    checks.require(isinstance(parsed, dict), _check("plugin_manifest_object"))
    version = parsed.get("version") if isinstance(parsed, dict) else None
    checks.require(isinstance(version, str), _check("plugin_version_present"))
    return version if isinstance(version, str) else ""


def _validate_cache(layout: NativeLayout, home: Path, checks: Checks) -> Path:
    version = _manifest_version(layout.source_root, checks)
    cache = home / "plugins" / "cache" / _MARKETPLACE / "codex-must-work" / version
    checks.require(cache.resolve(strict=True) == cache, _check("cache_identity_exact"))
    package = _LOAD_JSON((layout.source_root / "runtime" / "package-files.json").read_text("utf-8"))
    checks.require(isinstance(package, list), _check("package_manifest_array"))
    expected = (
        tuple(item for item in package if isinstance(item, str))
        if isinstance(package, list)
        else ()
    )
    checks.require(
        isinstance(package, list) and len(expected) == len(package), _check("package_paths_text")
    )
    actual = tuple(
        sorted(
            (path.relative_to(cache).as_posix() for path in cache.rglob("*") if path.is_file()),
            key=str.encode,
        )
    )
    checks.require(actual == tuple(sorted(expected, key=str.encode)), _check("cache_package_exact"))
    for path in (cache, *cache.rglob("*")):
        metadata = path.lstat()
        direct = not stat.S_ISLNK(metadata.st_mode) and path.resolve(strict=True) == path
        checks.require(direct, _check("cache_entries_direct"))
        expected_mode = 0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600
        checks.require(stat.S_IMODE(metadata.st_mode) == expected_mode, _check("cache_modes_exact"))
        checks.require(metadata.st_uid == os.geteuid(), _check("cache_owner_exact"))
        checks.require(not os.listxattr(path), _check("cache_xattrs_absent"))
        if stat.S_ISREG(metadata.st_mode):
            checks.require(metadata.st_nlink == 1, _check("cache_files_single_link"))
    return cache


def _validate_config(home: Path, cache: Path, checks: Checks) -> None:
    path = home / "config.toml"
    metadata = path.lstat()
    checks.require(stat.S_ISREG(metadata.st_mode), _check("config_regular"))
    checks.require(metadata.st_nlink == 1, _check("config_single_link"))
    checks.require(stat.S_IMODE(metadata.st_mode) == 0o600, _check("config_mode_exact"))
    parsed = _LOAD_TOML(path.read_text("utf-8"))
    features = parsed.get("features")
    marketplace = parsed.get("marketplaces")
    plugins = parsed.get("plugins")
    hooks = parsed.get("hooks")
    checks.require(
        isinstance(features, dict) and features.get("plugins") is True,
        _check("plugins_feature_enabled"),
    )
    local = marketplace.get(_MARKETPLACE) if isinstance(marketplace, dict) else None
    checks.require(
        isinstance(local, dict) and local.get("source_type") == "local",
        _check("marketplace_type_exact"),
    )
    checks.require(
        isinstance(local, dict) and local.get("source") == str(cache),
        _check("marketplace_source_exact"),
    )
    plugin = plugins.get(_PLUGIN_ID) if isinstance(plugins, dict) else None
    checks.require(
        isinstance(plugin, dict) and plugin.get("enabled") is True, _check("plugin_enabled")
    )
    states = hooks.get("state") if isinstance(hooks, dict) else None
    prefix = f"{_PLUGIN_ID}:hooks/hooks.json:"
    owned = (
        {key: value for key, value in states.items() if key.startswith(prefix)}
        if isinstance(states, dict)
        else {}
    )
    checks.require(len(owned) == 3, _check("trusted_hook_count_exact"))
    for value in owned.values():
        trusted = value.get("trusted_hash") if isinstance(value, dict) else None
        checks.require(
            isinstance(value, dict) and value.get("enabled") is True, _check("trusted_hook_enabled")
        )
        checks.require(
            isinstance(trusted, str) and trusted.startswith("sha256:") and len(trusted) == 71,
            _check("trusted_hook_hash_shape"),
        )


def _first_install(layout: NativeLayout, home: Path, checks: Checks) -> Path:
    config = home / "config.toml"
    _ = config.write_text('marker = "preserve"\n', encoding="utf-8", newline="\n")
    config.chmod(0o600)
    before = config.lstat()
    result = run_install(layout, home)
    checks.record_exit(result.returncode)
    checks.require(result.returncode == 0, _check("first_install_exit"))
    checks.require(result.stdout == "install=ok\n", _check("first_install_stdout_exact"))
    checks.require(result.stderr == "", _check("first_install_stderr_empty"))
    after = config.lstat()
    checks.require(
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino),
        _check("config_atomically_replaced"),
    )
    checks.require(
        'marker = "preserve"' in config.read_text("utf-8"), _check("config_prior_data_preserved")
    )
    cache = _validate_cache(layout, home, checks)
    _validate_config(home, cache, checks)
    data = home / "plugins" / "data" / "codex-must-work-codex-must-work-local"
    checks.require(data.resolve(strict=True) == data, _check("plugin_data_identity_exact"))
    checks.require(stat.S_IMODE(data.lstat().st_mode) == 0o700, _check("plugin_data_mode_exact"))
    stage = home / "plugins" / ".cmw-install-staging" / "codex-must-work"
    checks.require(not stage.exists() or not any(stage.iterdir()), _check("cache_staging_clean"))
    checks.require(not tuple(home.glob(".config.toml.cmw.*")), _check("config_staging_clean"))
    checks.require(bootstrap_clean(layout), _check("first_install_bootstrap_clean"))
    return cache


def _serialized_reinstalls(layout: NativeLayout, home: Path, checks: Checks) -> None:
    before = tree_snapshot(home)
    marker = layout.root / "installer-a-holds-lock"
    first = start_install(layout, home, marker)
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 60
        while not marker.is_dir() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        checks.require(marker.is_dir(), _check("lock_holder_started"))
        started = time.monotonic()
        second = start_install(layout, home)
        first_output, first_error = first.communicate(timeout=180)
        second_output, second_error = second.communicate(timeout=180)
        elapsed = time.monotonic() - started
        first_exit = first.returncode
        second_exit = second.returncode
    finally:
        stop_process(first)
        if second is not None:
            stop_process(second)
    checks.record_exit(first_exit)
    checks.require(
        first_exit == 0 and first_output == "install=ok\n" and first_error == "",
        _check("lock_first_install_ok"),
    )
    checks.record_exit(second_exit)
    checks.require(
        second_exit == 0 and second_output == "install=ok\n" and second_error == "",
        _check("lock_second_install_ok"),
    )
    checks.require(elapsed > 11.0, _check("lock_handoff_exceeds_eleven_seconds"))
    checks.require(tree_snapshot(home) == before, _check("reinstalls_are_no_write"))
    checks.require(bootstrap_clean(layout), _check("reinstall_bootstrap_clean"))


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


def _extract_bundled_runtime(installed: InstalledPlugin, target: str, checks: Checks) -> Path:
    layout, home, cache = installed.layout, installed.home, installed.cache
    runtime_version, target_name = _runtime_details(layout, target, checks)
    python_version = runtime_version.split("+", maxsplit=1)[0]
    data = home / "plugins" / "data" / "codex-must-work-codex-must-work-local"
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
        bool(stat.S_IMODE(metadata.st_mode) & 0o111), _check("bundled_python_executable")
    )
    checks.require(
        not tuple(data.glob(".portable-python-stage.*")), _check("runtime_staging_clean")
    )
    checks.require(not (data / ".portable-python.lock").exists(), _check("runtime_lock_clean"))
    return data


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


def _run_inactive_hooks(installed: InstalledPlugin, data: Path, checks: Checks) -> int:
    layout, home, cache = installed.layout, installed.home, installed.cache
    audit = layout.root / "audit"
    audit.mkdir(mode=0o700)
    create_audit_site(audit)
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
                result.stdout == "" and result.stderr == "", _check("inactive_hook_silent")
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


def run_smoke(source_root: Path, checks: Checks) -> int:
    checks.require(source_root.resolve(strict=True) == source_root, _check("source_root_direct"))
    checks.require((source_root / "install.sh").is_file(), _check("install_entrypoint_present"))
    target = _native_target(checks)
    allocation, layout = create_layout(source_root)
    try:
        checks.require(
            not any(layout.command_bin.glob("python*")), _check("child_path_has_no_python")
        )
        _unsafe_runtime_case(layout, RuntimeKind.SYMLINK, checks)
        _unsafe_runtime_case(layout, RuntimeKind.HARDLINK, checks)
        home = create_home(layout, "happy-home")
        cache = _first_install(layout, home, checks)
        _serialized_reinstalls(layout, home, checks)
        installed = InstalledPlugin(layout, home, cache)
        data = _extract_bundled_runtime(installed, target, checks)
        command_count = _run_inactive_hooks(installed, data, checks)
        checks.require(bootstrap_clean(layout), _check("final_bootstrap_clean"))
        return command_count
    finally:
        allocation.cleanup()


def main() -> int:
    checks = Checks()
    try:
        source_root = Path(__file__).absolute().parent.parent
        command_count = run_smoke(source_root, checks)
    except SmokeFailureError as failure:
        output = "\n".join(
            (
                "smoke_ok=false",
                f"{failure.check}=false",
                f"check_count={failure.count}",
                f"last_exit={failure.last_exit}",
                "",
            )
        )
        _ = sys.stdout.write(output)
        return 1
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
        output = "\n".join(
            (
                "smoke_ok=false",
                "unexpected_failure=true",
                f"check_count={checks.count}",
                f"last_exit={checks.last_exit}",
                "",
            )
        )
        _ = sys.stdout.write(output)
        return 1
    output = "\n".join(
        (
            "smoke_ok=true",
            f"check_count={checks.count}",
            "first_install_exit=0",
            "lock_first_exit=0",
            "lock_second_exit=0",
            f"inactive_command_count={command_count}",
            "",
        )
    )
    _ = sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
