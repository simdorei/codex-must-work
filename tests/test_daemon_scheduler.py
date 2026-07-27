from __future__ import annotations

from threading import Event, Lock, Thread, get_ident
from typing import final

import pytest

from scripts.daemon_scheduler import DeadlineScheduler, SchedulerError, SchedulerKey


@final
class _Clock:
    def __init__(self) -> None:
        self._lock = Lock()
        self._value = 0.0

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value


def test_reschedule_never_revalidates_an_older_same_key_entry() -> None:
    # Given
    clock = _Clock()
    scheduler = DeadlineScheduler(clock=clock)
    current_ran = Event()
    stale_ran = Event()
    poke_ran = Event()

    def reschedule() -> None:
        scheduler.schedule(SchedulerKey("task"), 20.0, lambda: None)
        current_ran.set()

    try:
        scheduler.schedule(SchedulerKey("task"), 10.0, stale_ran.set)
        scheduler.schedule(SchedulerKey("task"), 0.0, reschedule)
        assert current_ran.wait(1.0)

        # When
        clock.set(11.0)
        scheduler.wake(SchedulerKey("poke"), poke_ran.set)
        assert poke_ran.wait(1.0)

        # Then
        assert not stale_ran.is_set()
    finally:
        scheduler.close()


def test_cancelled_entry_never_runs_after_same_key_is_reused() -> None:
    # Given
    clock = _Clock()
    scheduler = DeadlineScheduler(clock=clock)
    stale_ran = Event()
    current_ran = Event()

    try:
        scheduler.schedule(SchedulerKey("task"), 5.0, stale_ran.set)
        scheduler.cancel(SchedulerKey("task"))
        scheduler.schedule(SchedulerKey("task"), 0.0, current_ran.set)
        assert current_ran.wait(1.0)

        # When
        clock.set(6.0)
        scheduler.wake(SchedulerKey("poke"), lambda: None)

        # Then
        assert not stale_ran.wait(0.1)
    finally:
        scheduler.close()


def test_callback_failure_does_not_kill_worker_before_later_callback() -> None:
    # Given
    scheduler = DeadlineScheduler()
    callback_started = Event()
    later_callback_ran = Event()
    worker_ids: list[int] = []

    def failing_callback() -> None:
        worker_ids.append(get_ident())
        callback_started.set()
        message = "callback failed"
        raise RuntimeError(message)

    try:
        scheduler.wake(SchedulerKey("failing"), failing_callback)
        assert callback_started.wait(1.0)

        # When
        def later_callback() -> None:
            worker_ids.append(get_ident())
            later_callback_ran.set()

        scheduler.wake(SchedulerKey("later"), later_callback)

        # Then
        assert later_callback_ran.wait(1.0), "a callback failure must not kill the scheduler worker"
        assert worker_ids[0] == worker_ids[1]
    finally:
        scheduler.close()


def test_callback_failure_is_bounded_and_success_recovers_degraded_health() -> None:
    # Given
    scheduler = DeadlineScheduler()
    callback_started = Event()
    later_callback_started = Event()
    release_later_callback = Event()
    later_callback_finished = Event()

    def failing_callback() -> None:
        callback_started.set()
        message = "secret callback payload"
        raise RuntimeError(message)

    def later_callback() -> None:
        later_callback_started.set()
        assert release_later_callback.wait(1.0)
        later_callback_finished.set()

    try:
        scheduler.wake(SchedulerKey("failing"), failing_callback)
        assert callback_started.wait(1.0)
        scheduler.wake(SchedulerKey("later"), later_callback)
        assert later_callback_started.wait(1.0)

        # When
        degraded = scheduler.status()

        # Then
        assert degraded.health == "degraded"
        assert degraded.callback_failure_count == 1
        assert degraded.last_callback_error == "callback_failed"
        assert "secret callback payload" not in str(degraded)

        release_later_callback.set()
        assert later_callback_finished.wait(1.0)
        assert scheduler.health == "healthy"
        assert scheduler.callback_failure_count == 1
    finally:
        scheduler.close()


def test_fatal_loop_rejects_subsequent_scheduling() -> None:
    # Given
    clock_ready = Event()
    fail_clock = Event()

    def clock() -> float:
        if fail_clock.is_set():
            message = "secret scheduler payload"
            raise RuntimeError(message)
        clock_ready.set()
        return 0.0

    scheduler = DeadlineScheduler(clock=clock)
    try:
        scheduler.schedule(SchedulerKey("future"), 10.0, lambda: None)
        assert clock_ready.wait(1.0)

        # When
        fail_clock.set()
        scheduler.schedule(SchedulerKey("fatal"), 0.0, lambda: None)
        for _ in range(100):
            if scheduler.health == "fatal":
                break
            _ = Event().wait(0.01)

        # Then
        assert scheduler.health == "fatal"
        assert scheduler.last_callback_error == "scheduler_fatal"
        with pytest.raises(SchedulerError) as raised:
            scheduler.schedule(SchedulerKey("later"), 0.0, lambda: None)
        assert str(raised.value) == "scheduler_unavailable"
    finally:
        scheduler.close()


def test_one_hundred_ordered_callbacks() -> None:
    # Given
    scheduler = DeadlineScheduler()
    trace: list[int] = []
    trace_lock = Lock()
    completed = Event()

    def callback(index: int) -> None:
        with trace_lock:
            trace.append(index)
            if len(trace) == 100:
                completed.set()

    try:
        # When
        for index in range(100):
            scheduler.schedule(
                SchedulerKey(f"item-{index}"),
                0.0,
                lambda index=index: callback(index),
            )

        # Then
        assert completed.wait(1.0)
        assert trace == list(range(100))
    finally:
        scheduler.close()


def test_callback_error_status_does_not_include_attacker_controlled_type() -> None:
    # Given
    scheduler = DeadlineScheduler()
    callback_started = Event()
    error_type = type("attacker_controlled_secret_type", (RuntimeError,), {})

    def failing_callback() -> None:
        callback_started.set()
        message = "secret callback message"
        raise error_type(message)

    try:
        scheduler.wake(SchedulerKey("sensitive"), failing_callback)
        assert callback_started.wait(1.0)

        # When
        status = scheduler.status()
        for _ in range(100):
            status = scheduler.status()
            if status.health == "degraded":
                break
            _ = Event().wait(0.01)

        # Then
        assert status.last_callback_error == "callback_failed"
        assert "attacker_controlled_secret_type" not in str(status)
        assert "secret callback message" not in str(status)
    finally:
        scheduler.close()


def test_concurrent_close_waits_for_worker_completion() -> None:
    # Given
    scheduler = DeadlineScheduler()
    callback_started = Event()
    release_callback = Event()
    first_started = Event()
    second_started = Event()
    first_finished = Event()
    second_finished = Event()

    def blocking_callback() -> None:
        callback_started.set()
        assert release_callback.wait(1.0)

    def close_first() -> None:
        first_started.set()
        scheduler.close()
        first_finished.set()

    def close_second() -> None:
        second_started.set()
        scheduler.close()
        second_finished.set()

    try:
        scheduler.wake(SchedulerKey("blocking"), blocking_callback)
        assert callback_started.wait(1.0)

        # When
        first_thread = Thread(target=close_first)
        first_thread.start()
        assert first_started.wait(1.0)
        assert not first_finished.wait(0.1)
        second_thread = Thread(target=close_second)
        second_thread.start()
        assert second_started.wait(1.0)

        # Then
        assert not second_finished.wait(0.1)
        release_callback.set()
        assert first_finished.wait(1.0)
        assert second_finished.wait(1.0)
        first_thread.join()
        second_thread.join()
    finally:
        release_callback.set()
        scheduler.close()


def test_base_exception_callback_failure_is_fatal() -> None:
    for error_type in (SystemExit, KeyboardInterrupt, GeneratorExit):
        # Given
        scheduler = DeadlineScheduler()
        callback_started = Event()

        def failing_callback(
            callback_started: Event = callback_started,
            error_type: type[BaseException] = error_type,
        ) -> None:
            callback_started.set()
            raise error_type()

        try:
            scheduler.wake(SchedulerKey("fatal-callback"), failing_callback)
            assert callback_started.wait(1.0)

            # When
            for _ in range(100):
                if scheduler.health == "fatal":
                    break
                _ = Event().wait(0.01)

            # Then
            assert scheduler.health == "fatal"
            with pytest.raises(SchedulerError, match="scheduler_unavailable"):
                scheduler.schedule(SchedulerKey("rejected"), 0.0, lambda: None)
        finally:
            scheduler.close()
