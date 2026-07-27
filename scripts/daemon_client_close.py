"""Run retryable daemon client close attempts."""

from __future__ import annotations

import threading
from typing import Protocol, final


class Closable(Protocol):
    """Expose the client operation serialized by the gate."""

    def close(self) -> None:
        """Close the owned client."""
        ...


@final
class CloseGate:
    """Retain one authoritative close attempt across caller interruption."""

    __slots__ = ("_attempt", "_condition", "_error", "_succeeded")

    def __init__(self) -> None:
        """Create an idle close gate."""
        self._condition = threading.Condition()
        self._attempt: threading.Thread | None = None
        self._error: BaseException | None = None
        self._succeeded = False

    def finish(self, client: Closable) -> bool:
        """Finish or retry closing one client outside caller-owned locks."""
        with self._condition:
            while True:
                attempt = self._attempt
                if attempt is not None and attempt.is_alive():
                    _ = self._condition.wait_for(
                        lambda attempt=attempt: not attempt.is_alive(),
                        timeout=0.05,
                    )
                    continue
                if self._succeeded:
                    return True
                if self._error is not None:
                    error = self._error
                    self._attempt = None
                    self._error = None
                    raise error
                attempt = threading.Thread(
                    target=self._worker,
                    args=(client,),
                    name="daemon-client-close",
                )
                self._attempt = attempt
                attempt.start()
                _ = self._condition.wait_for(
                    lambda attempt=attempt: not attempt.is_alive(),
                    timeout=0.05,
                )

    def _worker(self, client: Closable) -> None:
        error: BaseException | None = None
        try:
            try:
                self._close_attempt(client)
            except BaseException as attempt_error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
                error = attempt_error
        except BaseException as terminal_error:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            error = terminal_error
        finally:
            with self._condition:
                self._error = error
                self._condition.notify_all()

    def _close_attempt(self, client: Closable) -> None:
        client.close()
        self._succeeded = True
