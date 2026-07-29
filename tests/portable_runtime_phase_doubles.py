from __future__ import annotations

from typing import final


@final
class StreamDouble:
    def __init__(self, *, fail_first_close: bool = False) -> None:
        self.closed = False
        self.close_calls = 0
        self._fail_first_close = fail_first_close

    def close(self) -> None:
        self.close_calls += 1
        if self._fail_first_close and self.close_calls == 1:
            reason = "stdin-close-injected"
            raise OSError(reason)
        self.closed = True


@final
class ThreadDouble:
    def __init__(
        self,
        *,
        name: str = "reader-double",
        fail_start: bool = False,
        fail_join: bool = False,
    ) -> None:
        self.name = name
        self.ident: int | None = None
        self.join_calls = 0
        self._fail_start = fail_start
        self._fail_join = fail_join

    def start(self) -> None:
        if self._fail_start:
            reason = "reader-start-injected"
            raise RuntimeError(reason)
        self.ident = 1

    def join(self, timeout: float) -> None:
        _ = timeout
        self.join_calls += 1
        if self._fail_join:
            reason = "reader-join-injected"
            raise RuntimeError(reason)

    def is_alive(self) -> bool:
        return False


@final
class ProcessDouble:
    def __init__(self, *, stdin_close_failure: bool = False, close_failure: bool = False) -> None:
        self.stdin = StreamDouble(fail_first_close=stdin_close_failure)
        self.stdout = StreamDouble()
        self.stderr = StreamDouble()
        self.close_calls = 0
        self._close_failure = close_failure

    def close(self) -> None:
        self.close_calls += 1
        for stream in (self.stdin, self.stdout, self.stderr):
            stream.close()
        if self._close_failure:
            reason = "process-close-injected"
            raise RuntimeError(reason)
