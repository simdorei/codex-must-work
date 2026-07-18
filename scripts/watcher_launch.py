"""Launch the singleton watcher process from hooks or the resident manager."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

_WINDOWS_DETACHED_FLAGS: Final = 0x08000008


def launch_watcher() -> None:
    """Start a detached watcher; its lease makes duplicate launches harmless."""
    command = [sys.executable, str(Path(__file__).with_name("watcher.py").resolve())]
    if os.name == "nt":
        _ = subprocess.Popen(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_WINDOWS_DETACHED_FLAGS,
        )
        return
    _ = subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
