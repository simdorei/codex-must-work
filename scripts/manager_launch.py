"""Launch the resident manager and verify its readiness handshake."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import override

from scripts.manager_runtime import load_manager_runtime, request_manager_startup_cancel

_WINDOWS_DETACHED_FLAGS = 0x08000008
MANAGER_INITIALIZATION_BUDGET_SECONDS = 38.0
MANAGER_READY_TIMEOUT_SECONDS = 60.0
_CLEANUP_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ManagerLaunchError(RuntimeError):
    """Report a failed resident-manager readiness handshake."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


def launch_manager(
    root: Path,
    runtime_file: Path,
    timeout_seconds: float = MANAGER_READY_TIMEOUT_SECONDS,
) -> int:
    """Start one detached manager and wait for verified app-server readiness."""
    command = [sys.executable, str(_manager_script()), runtime_file.name]
    creation_flags = _WINDOWS_DETACHED_FLAGS if os.name == "nt" else 0
    try:
        process = _spawn_manager(command, creation_flags)
    except OSError as error:
        message = "manager_process_start_failed"
        raise ManagerLaunchError(message) from error
    try:
        return _wait_until_ready(root, runtime_file, process, timeout_seconds)
    except ManagerLaunchError as error:
        if error.reason_code == "manager_ready_timeout":
            request_manager_startup_cancel(root, runtime_file)
            _wait_for_cleanup(process)
        raise


def _manager_script() -> Path:
    return Path(__file__).with_name("manager.py").resolve()


def _spawn_manager(command: list[str], creation_flags: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
        start_new_session=os.name != "nt",
    )


def _wait_until_ready(
    root: Path,
    runtime_file: Path,
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        runtime = load_manager_runtime(root, runtime_file.name)
        if runtime is None:
            message = "manager_runtime_disappeared"
            raise ManagerLaunchError(message)
        if runtime.manager_error is not None:
            raise ManagerLaunchError(runtime.manager_error)
        if runtime.manager_ready:
            return process.pid
        if process.poll() is not None:
            message = "manager_process_exited"
            raise ManagerLaunchError(message)
        time.sleep(0.1)
    message = "manager_ready_timeout"
    raise ManagerLaunchError(message)


def _wait_for_cleanup(process: subprocess.Popen[bytes]) -> None:
    """Allow Python finally blocks to restore Goal state; never hard-kill them."""
    if process.poll() is not None:
        return
    try:
        _ = process.wait(timeout=_CLEANUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return
