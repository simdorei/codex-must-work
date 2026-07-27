"""Serialize first-time creation of the daemon's private plugin-data root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

from scripts.daemon_reservation import KeyedReservations
from scripts.private_root import ensure_private_root

if TYPE_CHECKING:
    import threading
    from pathlib import Path

_ROOT_INITIALIZATION: Final = "private-plugin-root"


@final
class PrivateRootInitializer:
    """Single-flight private-root verification without holding the service lock."""

    def __init__(self, root: Path, lock: threading.RLock) -> None:
        """Bind one root to a lock-free-I/O reservation."""
        self._root = root
        self._reservations = KeyedReservations(lock)

    def ensure(self) -> None:
        """Create or verify the root through one initialization flight."""
        with self._reservations.claim(_ROOT_INITIALIZATION):
            ensure_private_root(self._root)
