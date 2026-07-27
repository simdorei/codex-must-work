"""Construct daemon-owned Codex resources without runtime side effects on import."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from scripts.app_server_client import ResidentAppServer
from scripts.codex_executable import resolve_codex_executable

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.app_server_protocol import AppServerActivity
    from scripts.daemon_client_pool import ClosableAppServer


def resident_client(
    fingerprint: str,
    listener: Callable[[AppServerActivity], None],
) -> ClosableAppServer:
    """Create the one lazy shared app-server child used by the daemon."""
    return ResidentAppServer(fingerprint, activity_listener=listener)


def codex_fingerprint() -> str:
    """Hash the trusted Codex executable before managed ownership begins."""
    return hashlib.sha256(resolve_codex_executable().read_bytes()).hexdigest()
