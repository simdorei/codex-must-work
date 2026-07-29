"""Exception-safe cleanup for acquired native protocol resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.portable_runtime_windows_ownership import CleanupFailure

if TYPE_CHECKING:
    import threading

    from tests.portable_runtime_windows_job import WindowsJobProcess


def cleanup_protocol_resources(
    process: WindowsJobProcess,
    readers: list[threading.Thread],
) -> tuple[CleanupFailure, ...]:
    failures: list[CleanupFailure] = []
    try:
        process.close()
    except (OSError, RuntimeError) as error:
        failures.append(CleanupFailure("close(process)", error))
    for thread in readers:
        if thread.ident is None:
            continue
        try:
            thread.join(timeout=10)
        except RuntimeError as error:
            failures.append(CleanupFailure(f"join({thread.name})", error))
        else:
            if thread.is_alive():
                reason = f"reader still alive: {thread.name}"
                failures.append(
                    CleanupFailure(
                        f"join({thread.name})",
                        RuntimeError(reason),
                    )
                )
    return tuple(failures)
