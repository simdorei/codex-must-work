# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run one detached resident manager for an opted-in Codex thread."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.app_server_client import ResidentAppServer
from scripts.app_server_protocol import AppServerProtocolError
from scripts.codex_executable import CodexExecutableError
from scripts.goal_control import GoalControlError
from scripts.manager_engine import ManagerEngine
from scripts.manager_lease import acquire_manager_lease, release_manager_lease
from scripts.manager_runtime import (
    load_manager_runtime,
    mark_manager_stopped,
    record_manager_failure,
)
from scripts.manager_runtime_values import ManagerRuntimeError
from scripts.private_root import ensure_private_root
from scripts.state import StateError, state_root

_POLL_SECONDS = 1.0
_EXPECTED_ARGUMENT_COUNT = 2

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.manager_lease import ManagerLease

_HANDLED_ERRORS = (
    AppServerProtocolError,
    CodexExecutableError,
    GoalControlError,
    ManagerRuntimeError,
    OSError,
    StateError,
    ValueError,
)


def run_manager(runtime_name: str) -> int:
    """Own and supervise one hashed runtime until it is disabled."""
    root = state_root()
    ensure_private_root(root)
    lease = acquire_manager_lease(root, runtime_name)
    if lease is None:
        return 0
    client: ResidentAppServer | None = None
    engine: ManagerEngine | None = None
    exit_code = 0
    try:
        initial_runtime = load_manager_runtime(root, runtime_name)
        if initial_runtime is not None:
            client = ResidentAppServer(initial_runtime.executable_sha256)
            engine = ManagerEngine(root, runtime_name, client, pid=os.getpid())
            engine.initialize()
            while engine.tick():
                time.sleep(_POLL_SECONDS)
    except _HANDLED_ERRORS as error:
        _record_failure_if_present(root, runtime_name, error)
        _ = sys.stderr.write(f"Codex Must Work manager failed: {error}\n")
        exit_code = 1
    finally:
        cleanup_errors = _cleanup(root, runtime_name, engine, client, lease)
        if cleanup_errors:
            try:
                _record_failure_if_present(root, runtime_name, cleanup_errors[0])
            except _HANDLED_ERRORS as error:
                cleanup_errors.append(error)
        for error in cleanup_errors:
            _ = sys.stderr.write(f"Codex Must Work manager cleanup failed: {error}\n")
        if cleanup_errors:
            exit_code = 1
    return exit_code


def _record_failure_if_present(root: Path, runtime_name: str, error: Exception) -> None:
    runtime = load_manager_runtime(root, runtime_name)
    if runtime is not None:
        record_manager_failure(root, runtime.runtime_file, _failure_reason(error))


def _failure_reason(error: Exception) -> str:
    if isinstance(error, (CodexExecutableError, GoalControlError, ManagerRuntimeError)):
        return error.reason_code
    return "app_server_failed"


def _cleanup(
    root: Path,
    runtime_name: str,
    engine: ManagerEngine | None,
    client: ResidentAppServer | None,
    lease: ManagerLease,
) -> list[Exception]:
    errors: list[Exception] = []
    engine_close = engine.close if engine is not None else _no_cleanup
    client_close = client.close if client is not None else _no_cleanup
    try:
        _capture_cleanup_error(engine_close, errors)
    finally:
        try:
            _capture_cleanup_error(
                lambda: _mark_stopped_if_present(root, runtime_name),
                errors,
            )
        finally:
            try:
                _capture_cleanup_error(client_close, errors)
            finally:
                _capture_cleanup_error(lambda: release_manager_lease(lease), errors)
    return errors


def _capture_cleanup_error(action: Callable[[], None], errors: list[Exception]) -> None:
    try:
        action()
    except _HANDLED_ERRORS as error:
        errors.append(error)


def _no_cleanup() -> None:
    return


def _mark_stopped_if_present(root: Path, runtime_name: str) -> None:
    runtime = load_manager_runtime(root, runtime_name)
    if runtime is not None:
        mark_manager_stopped(root, runtime.runtime_file)


def _main() -> int:
    if len(sys.argv) != _EXPECTED_ARGUMENT_COUNT:
        _ = sys.stderr.write("usage: manager.py <hashed-runtime-name>\n")
        return 2
    return run_manager(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(_main())
