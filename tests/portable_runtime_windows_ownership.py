"""Typed LIFO ownership for exceptional Windows process cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, NoReturn, final, override

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    label: str
    error: Exception

    def render(self) -> str:
        return f"{self.label}: {self.error}"


@final
class ResourceCleanupError(RuntimeError):
    primary: Exception
    cleanup_failures: tuple[CleanupFailure, ...]
    _recovery: ResourceLedger | None

    def __init__(
        self,
        primary: Exception,
        cleanup_failures: tuple[CleanupFailure, ...],
        recovery: ResourceLedger | None = None,
    ) -> None:
        self.primary = primary
        self.cleanup_failures = cleanup_failures
        self._recovery = recovery
        super().__init__(str(self))

    @property
    def pending_ownership(self) -> tuple[PendingOwnership, ...]:
        if self._recovery is None:
            return ()
        return self._recovery.pending_ownership()

    def retry_cleanup(self) -> None:
        """Attempt every still-owned action once and retain any failures."""
        if self._recovery is None:
            return
        failures = self._recovery.cleanup()
        if failures:
            raise ResourceCleanupError(
                self.primary,
                failures,
                self._recovery,
            ) from self.primary

    @override
    def __str__(self) -> str:
        cleanup = "; ".join(failure.render() for failure in self.cleanup_failures)
        return f"{self.primary}; cleanup failures: {cleanup}"


@dataclass(frozen=True, slots=True)
class _CleanupAction:
    label: Callable[[], str]
    callback: Callable[[], None]


@dataclass(frozen=True, slots=True)
class PendingOwnership:
    key: str
    label: str


@final
class ProcessLifecycleOwner:
    """Retain one process identity until termination and handle close both succeed."""

    __slots__ = ("close", "handle", "terminate", "termination_complete")

    def __init__(
        self,
        handle: int,
        terminate: Callable[[int], None],
        close: Callable[[int], None],
    ) -> None:
        self.handle = handle
        self.terminate = terminate
        self.close = close
        self.termination_complete = False

    @property
    def pending_label(self) -> str:
        if self.termination_complete:
            return "CloseHandle(terminated-process)"
        return "TerminateProcess+CloseHandle(process)"

    def cleanup(self) -> None:
        if not self.termination_complete:
            self.terminate(self.handle)
            self.termination_complete = True
        self.close(self.handle)
        self.handle = 0


class ResourceLedger:
    """Register ownership immediately and release it in reverse order."""

    def __init__(self) -> None:
        self._actions: dict[str, _CleanupAction] = {}
        self._order: list[str] = []

    def register(
        self,
        key: str,
        label: str,
        callback: Callable[..., None],
        *arguments: int,
    ) -> None:
        if key in self._actions:
            reason = f"duplicate resource key: {key}"
            raise RuntimeError(reason)
        self._actions[key] = _CleanupAction(
            lambda: label,
            partial(callback, *arguments),
        )
        self._order.append(key)

    def register_process_lifecycle(
        self,
        key: str,
        owner: ProcessLifecycleOwner,
    ) -> None:
        if key in self._actions:
            reason = f"duplicate resource key: {key}"
            raise RuntimeError(reason)
        self._actions[key] = _CleanupAction(
            lambda: owner.pending_label,
            owner.cleanup,
        )
        self._order.append(key)

    def replace(
        self,
        key: str,
        label: str,
        callback: Callable[[], None],
    ) -> None:
        if key not in self._actions:
            reason = f"missing resource key: {key}"
            raise RuntimeError(reason)
        self._actions[key] = _CleanupAction(lambda: label, callback)

    def discard(self, key: str) -> None:
        _ = self._actions.pop(key)

    def close_now(self, key: str) -> None:
        action = self._actions[key]
        action.callback()
        _ = self._actions.pop(key)

    def cleanup(self) -> tuple[CleanupFailure, ...]:
        failures: list[CleanupFailure] = []
        for key in reversed(self._order):
            action = self._actions.get(key)
            if action is None:
                continue
            try:
                action.callback()
            except (OSError, RuntimeError) as error:
                failures.append(CleanupFailure(action.label(), error))
            else:
                _ = self._actions.pop(key)
        return tuple(failures)

    def pending_ownership(self) -> tuple[PendingOwnership, ...]:
        """Return exact retained owners in deterministic cleanup order."""
        return tuple(
            PendingOwnership(key, self._actions[key].label())
            for key in reversed(self._order)
            if key in self._actions
        )


def raise_with_cleanup(
    primary: Exception,
    failures: tuple[CleanupFailure, ...],
    recovery: ResourceLedger | None = None,
) -> NoReturn:
    if failures:
        raise ResourceCleanupError(primary, failures, recovery) from primary
    raise primary


def raise_collected(errors: list[CleanupFailure]) -> None:
    if not errors:
        return
    primary, *cleanup = errors
    if cleanup:
        raise ResourceCleanupError(primary.error, tuple(cleanup)) from primary.error
    raise primary.error


LEDGER_KEYS: Final = (
    "job",
    "process-lifecycle",
    "thread",
    "stdin-child",
    "stdin-parent",
    "stdout-parent",
    "stdout-child",
    "stderr-parent",
    "stderr-child",
)
