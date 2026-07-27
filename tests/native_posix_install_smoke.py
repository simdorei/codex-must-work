# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run tests/native_posix_install_smoke.py
"""Candidate-bound native installer smoke orchestration for POSIX CI."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.native_posix_hook_checks import (
    InstalledPlugin,
    extract_bundled_runtime,
    run_inactive_hooks,
)
from tests.native_posix_install_checks import first_install
from tests.native_posix_reinstall_checks import serialized_reinstalls
from tests.native_posix_runtime_checks import (
    RuntimeInspection,
    require_no_bytecode,
    validate_runtime_and_mcp,
)
from tests.native_posix_smoke_support import (
    CheckName,
    Checks,
    NativeLayout,
    RuntimeKind,
    SmokeFailureError,
    bootstrap_clean,
    create_home,
    create_layout,
    run_install,
)
from tests.native_posix_tree_snapshot import tree_snapshot


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


def run_smoke(source_root: Path, checks: Checks) -> int:
    """Run the complete native smoke against exactly this checkout."""
    checks.require(source_root.resolve(strict=True) == source_root, _check("source_root_direct"))
    checks.require((source_root / "install.sh").is_file(), _check("install_entrypoint_present"))
    target = _native_target(checks)
    allocation, layout = create_layout(source_root)
    try:
        checks.require(
            not any(layout.command_bin.glob("python*")),
            _check("child_path_has_no_python"),
        )
        _unsafe_runtime_case(layout, RuntimeKind.SYMLINK, checks)
        _unsafe_runtime_case(layout, RuntimeKind.HARDLINK, checks)
        home = create_home(layout, "happy-home")
        cache = first_install(layout, home, checks)
        data = home / "plugins" / "data" / "codex-must-work-codex-must-work-local"
        validate_runtime_and_mcp(
            RuntimeInspection(source_root, cache, data, target),
            checks,
        )
        serialized_reinstalls(layout, home, checks)
        require_no_bytecode(data, checks)
        installed = InstalledPlugin(layout, home, cache)
        data = extract_bundled_runtime(installed, target, checks)
        command_count = run_inactive_hooks(installed, data, checks)
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
            "first_install=true",
            "unsafe_runtime_rejected=true",
            "serialized_reinstall=true",
            "no_write_reinstall=true",
            "executable_mode=true",
            "capability_key_metadata=true",
            "mcp_import=true",
            f"inactive_command_count={command_count}",
            "",
        )
    )
    _ = sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
