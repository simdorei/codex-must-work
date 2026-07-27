"""Coordinate keyed operations without holding the shared lock during I/O."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Literal, final

if TYPE_CHECKING:
    from types import TracebackType


@final
class KeyedReservations:
    """Serialize only operations that use the same key."""

    def __init__(self, lock: threading.RLock) -> None:
        """Bind reservation waits to the owning service lock."""
        self._condition = threading.Condition(lock)
        self._active: set[str] = set()

    def claim(self, key: str) -> _Reservation:
        """Return a context that reserves one key while leaving I/O lock-free."""
        return _Reservation(self, key)

    def enter(self, key: str) -> None:
        """Wait for and reserve one key."""
        with self._condition:
            while key in self._active:
                _ = self._condition.wait()
            self._active.add(key)

    def leave(self, key: str) -> None:
        """Release one key and wake its next waiter."""
        with self._condition:
            self._active.remove(key)
            self._condition.notify_all()


@final
class _Reservation:
    def __init__(self, reservations: KeyedReservations, key: str) -> None:
        self._reservations = reservations
        self._key = key

    def __enter__(self) -> None:
        self._reservations.enter(self._key)

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        _ = error_type, error, traceback
        self._reservations.leave(self._key)
        return False
