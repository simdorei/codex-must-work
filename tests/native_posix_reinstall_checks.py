"""Serialized no-write reinstall checks for the native POSIX smoke."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tests.native_posix_smoke_support import (
    CheckName,
    Checks,
    NativeLayout,
    bootstrap_clean,
    start_install,
    stop_process,
)
from tests.native_posix_tree_snapshot import tree_snapshot

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path


def _check(name: str) -> CheckName:
    return CheckName(name)


def serialized_reinstalls(layout: NativeLayout, home: Path, checks: Checks) -> None:
    """Prove lock serialization and byte-for-byte no-write reinstalls."""
    control_key = home / "plugins" / "data" / "codex-must-work-simdorei" / "control.key"
    metadata_only = frozenset((control_key,))
    before = tree_snapshot(home, metadata_only=metadata_only)
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
    checks.require(
        tree_snapshot(home, metadata_only=metadata_only) == before,
        _check("reinstalls_are_no_write"),
    )
    checks.require(bootstrap_clean(layout), _check("reinstall_bootstrap_clean"))
