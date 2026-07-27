"""Run keyed callbacks only when signalled or when their deadline arrives."""

from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, NewType, final, override

if TYPE_CHECKING:
    from collections.abc import Callable


SchedulerKey = NewType("SchedulerKey", str)
_CALLBACK_ERROR_CODE = "callback_failed"
_FATAL_ERROR_CODE = "scheduler_fatal"


class SchedulerHealth(StrEnum):
    """Classify whether the scheduler can continue accepting work."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """Expose bounded scheduler health without callback traceback details."""

    health: SchedulerHealth
    callback_failure_count: int
    last_callback_error: str | None


@dataclass(frozen=True, slots=True)
class SchedulerError(RuntimeError):
    """Reject invalid scheduler lifecycle operations."""

    reason_code: str

    @override
    def __str__(self) -> str:
        return self.reason_code


@dataclass(frozen=True, order=True, slots=True)
class _Entry:
    deadline: float
    sequence: int
    key: SchedulerKey = field(compare=False)
    generation: int = field(compare=False)
    callback: Callable[[], None] = field(compare=False)


@final
class DeadlineScheduler:
    """Maintain one resident condition wait instead of a polling loop."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start one sleeping worker using the supplied monotonic clock."""
        self._clock = clock
        self._condition = threading.Condition()
        self._entries: list[_Entry] = []
        self._generations: dict[SchedulerKey, int] = {}
        self._sequence = 0
        self._closed = False
        self._health = SchedulerHealth.HEALTHY
        self._callback_failure_count = 0
        self._last_callback_error: str | None = None
        self._completion = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="cmw-deadline-scheduler",
            daemon=True,
        )
        self._thread.start()

    def schedule(
        self,
        key: SchedulerKey,
        deadline: float,
        callback: Callable[[], None],
    ) -> None:
        """Replace one keyed deadline and wake the resident wait."""
        if not key:
            reason = "scheduler_key_missing"
            raise SchedulerError(reason)
        with self._condition:
            if self._health is SchedulerHealth.FATAL:
                reason = "scheduler_unavailable"
                raise SchedulerError(reason)
            if self._closed:
                reason = "scheduler_closed"
                raise SchedulerError(reason)
            self._sequence += 1
            generation = self._sequence
            self._generations[key] = generation
            heapq.heappush(
                self._entries,
                _Entry(deadline, self._sequence, key, generation, callback),
            )
            self._condition.notify()

    def wake(self, key: SchedulerKey, callback: Callable[[], None]) -> None:
        """Coalesce an immediate signal under the supplied key."""
        self.schedule(key, self._clock(), callback)

    def cancel(self, key: SchedulerKey) -> None:
        """Invalidate a keyed callback without scanning the heap."""
        with self._condition:
            _ = self._generations.pop(key, None)
            self._condition.notify()

    def close(self) -> None:
        """Stop the scheduler and wait for its single worker to exit."""
        with self._condition:
            if not self._closed:
                self._closed = True
                self._generations.clear()
                self._entries.clear()
                self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            _ = self._completion.wait()

    def _run(self) -> None:
        try:
            while True:
                entry = self._next_entry()
                if entry is None:
                    return
                try:
                    entry.callback()
                except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
                    self._record_callback_failure()
                    continue
                self._record_callback_success()
        except BaseException:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            self._mark_fatal()
        finally:
            self._completion.set()

    @property
    def health(self) -> SchedulerHealth:
        """Return the current scheduler health classification."""
        with self._condition:
            return self._health

    @property
    def callback_failure_count(self) -> int:
        """Return the number of callbacks that failed on the worker."""
        with self._condition:
            return self._callback_failure_count

    @property
    def last_callback_error(self) -> str | None:
        """Return the bounded public code for the latest callback failure."""
        with self._condition:
            return self._last_callback_error

    def status(self) -> SchedulerStatus:
        """Return one consistent scheduler health snapshot."""
        with self._condition:
            return SchedulerStatus(
                self._health,
                self._callback_failure_count,
                self._last_callback_error,
            )

    def _record_callback_failure(self) -> None:
        with self._condition:
            self._callback_failure_count += 1
            self._last_callback_error = _CALLBACK_ERROR_CODE
            if self._health is SchedulerHealth.HEALTHY:
                self._health = SchedulerHealth.DEGRADED

    def _record_callback_success(self) -> None:
        with self._condition:
            if self._health is SchedulerHealth.DEGRADED:
                self._health = SchedulerHealth.HEALTHY

    def _mark_fatal(self) -> None:
        with self._condition:
            self._health = SchedulerHealth.FATAL
            self._entries.clear()
            self._generations.clear()
            self._last_callback_error = _FATAL_ERROR_CODE
            self._condition.notify_all()

    def _next_entry(self) -> _Entry | None:
        with self._condition:
            while not self._closed:
                self._discard_stale()
                if not self._entries:
                    _ = self._condition.wait()
                    continue
                entry = self._entries[0]
                remaining = entry.deadline - self._clock()
                if remaining > 0:
                    _ = self._condition.wait(remaining)
                    continue
                _ = heapq.heappop(self._entries)
                if self._generations.get(entry.key) != entry.generation:
                    continue
                _ = self._generations.pop(entry.key, None)
                return entry
            return None

    def _discard_stale(self) -> None:
        while self._entries:
            entry = self._entries[0]
            if self._generations.get(entry.key) == entry.generation:
                return
            _ = heapq.heappop(self._entries)
