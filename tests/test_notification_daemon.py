from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, final, override

import pytest

from scripts.durations import Milliseconds
from scripts.mcp_arguments import parse_start_request
from scripts.monitor_models import (
    DaemonServiceError,
    SessionId,
    SessionRequest,
    StartRequest,
    ToolResult,
)
from scripts.notification_daemon import NotificationDaemonService
from scripts.notifications import (
    LifecycleNotification,
    NotificationKind,
    NotificationSink,
)
from scripts.state import load_state, runtime_path

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


@final
class _RecordingSink(NotificationSink):
    def __init__(self) -> None:
        self.events: list[LifecycleNotification] = []
        self._lock = threading.Lock()

    @override
    def notify(self, event: LifecycleNotification) -> None:
        with self._lock:
            self.events.append(event)


@final
class _FailOnceWatcher:
    def __init__(self) -> None:
        self.failed = threading.Event()
        self.recovered = threading.Event()
        self.calls = 0

    def tick(self, _monotonic: float, _wall_time: datetime) -> bool:
        self.calls += 1
        if self.calls == 1:
            self.failed.set()
            raise OSError
        self.recovered.set()
        return True


def test_start_is_passive_and_writes_only_notification_state(
    tmp_path: Path,
) -> None:
    root, transcript = _paths(tmp_path)
    service = NotificationDaemonService(root=root)
    try:
        result = service.start(_request(transcript))
        values = load_state(root, runtime_path(root, "thread-1")).values
    finally:
        service.close()

    assert result.status == "active"
    assert result.enabled is True
    assert values["warning_after_ms"] == 300_000
    assert values["critical_after_ms"] == 600_000
    assert "restart_after_ms" not in values
    assert "restart_request" not in values
    assert "auto_restart_requested_by_user" not in values
    assert "managed_mode" not in values


def test_complete_emits_completion_and_removes_only_monitoring_state(
    tmp_path: Path,
) -> None:
    root, transcript = _paths(tmp_path)
    sink = _RecordingSink()
    service = NotificationDaemonService(root=root, notification_sink=sink)
    try:
        _ = service.start(_request(transcript))
        result = service.complete(SessionRequest(SessionId("thread-1")))
    finally:
        service.close()

    assert result.status == "completed"
    assert not runtime_path(root, "thread-1").exists()
    assert [event.kind for event in sink.events].count(NotificationKind.COMPLETED) == 1


def test_exact_start_is_reused_but_reconfiguration_is_rejected(tmp_path: Path) -> None:
    root, transcript = _paths(tmp_path)
    service = NotificationDaemonService(root=root)
    try:
        first = service.start(_request(transcript))
        second = service.start(_request(transcript))
        changed = _request(
            transcript,
            warning=Milliseconds(420_000),
            critical=Milliseconds(900_000),
        )
        with pytest.raises(
            DaemonServiceError,
            match="monitoring_reconfiguration_requires_work_off",
        ):
            _ = service.start(changed)
    finally:
        service.close()

    assert first.reused is False
    assert second.reused is True


def test_failed_tick_is_retried_and_status_is_degraded_until_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, transcript = _paths(tmp_path)
    watcher = _FailOnceWatcher()
    service = NotificationDaemonService(root=root)
    monkeypatch.setattr(service, "_watcher", watcher)
    try:
        _ = service.start(_request(transcript))
        assert watcher.failed.wait(1.0)
        status = _wait_for_status(service, "degraded")

        assert status.enabled is True
        assert status.daemon_error == "callback_failed"
        assert watcher.recovered.wait(2.0)
        assert _wait_for_status(service, "active").daemon_error is None
    finally:
        service.close()


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    _ = transcript.parent.mkdir(parents=True)
    _ = transcript.write_text("", encoding="utf-8")
    return root, transcript


def _request(
    transcript: Path,
    *,
    warning: Milliseconds | None = None,
    critical: Milliseconds | None = None,
) -> StartRequest:
    return parse_start_request(
        {
            "session_id": "thread-1",
            "activation_turn_id": "turn-1",
            "transcript_path": str(transcript),
            "warning_after_ms": int(warning or Milliseconds(300_000)),
            "critical_after_ms": int(critical or Milliseconds(600_000)),
        },
        None,
    ).request


def _wait_for_status(
    service: NotificationDaemonService,
    expected: str,
) -> ToolResult:
    deadline = time.monotonic() + 1.0
    request = SessionRequest(SessionId("thread-1"))
    while True:
        status = service.status(request)
        if status.status == expected:
            return status
        if time.monotonic() >= deadline:
            pytest.fail(f"expected {expected!r}, observed {status.status!r}")
        time.sleep(0.01)
