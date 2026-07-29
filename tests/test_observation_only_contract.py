from __future__ import annotations

import http.client
import json
import socket
import subprocess
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol, final

from scripts.durations import Milliseconds
from scripts.mcp_arguments import parse_start_request
from scripts.monitor_diagnostics import DiagnosticCode
from scripts.monitor_models import SessionId, SessionRequest
from scripts.notification_daemon import NotificationDaemonService
from scripts.notifications import LifecycleNotification, NotificationKind
from tests.watcher_fixture import WALL_TIME, diagnostic_codes

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from scripts.state_io import JsonValue


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _stdlib_json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final[_JsonLoader] = _stdlib_json_loader()


@final
class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[LifecycleNotification] = []

    def notify(self, event: LifecycleNotification) -> None:
        self.events.append(event)


def test_cmw_lifecycle_has_only_negative_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a monitored rollout, only lifecycle delivery and local diagnostics occur."""
    root, transcript = _paths(tmp_path)
    sink = _RecordingSink()
    forbidden_calls: list[str] = []
    service = NotificationDaemonService(root=root, notification_sink=sink)

    def forbidden(name: str) -> Callable[..., None]:
        def call(*_args: str, **_kwargs: str) -> None:
            forbidden_calls.append(name)
            raise AssertionError(name)

        return call

    monkeypatch.setattr(subprocess, "Popen", forbidden("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", forbidden("subprocess.run"))
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden("HTTPSConnection"))
    monkeypatch.setattr(socket, "socket", forbidden("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))

    def disable_schedule(*, immediate: bool) -> None:
        _ = immediate

    monkeypatch.setattr(service, "_schedule_tick", disable_schedule)
    request = parse_start_request(
        {
            "session_id": "thread-1",
            "activation_turn_id": "turn-1",
            "transcript_path": str(transcript),
            "warning_after_ms": int(Milliseconds(90_000)),
            "critical_after_ms": int(Milliseconds(300_000)),
        },
        None,
    ).request
    try:
        # cmw.work_on
        assert service.start(request).status == "active"
        assert service.observe(0.0, WALL_TIME) is True
        # warning -> critical, with a local heartbeat between them
        assert service.observe(91.0, WALL_TIME + timedelta(seconds=91)) is True
        assert service.observe(181.0, WALL_TIME + timedelta(seconds=181)) is True
        assert [event.kind for event in sink.events] == [NotificationKind.BOTTLENECK_SUSPECTED]
        assert service.observe(301.0, WALL_TIME + timedelta(seconds=301)) is True
        # cmw.complete
        assert service.complete(SessionRequest(SessionId("thread-1"))).status == "completed"
    finally:
        service.close()

    assert forbidden_calls == []
    assert [event.kind for event in sink.events] == [
        NotificationKind.BOTTLENECK_SUSPECTED,
        NotificationKind.BOTTLENECK_CRITICAL,
        NotificationKind.COMPLETED,
    ]
    assert DiagnosticCode.HEARTBEAT_ACTIVE.value in diagnostic_codes(root)
    diagnostic_bytes = (root / "logs" / "diagnostic.jsonl").read_bytes()
    assert b"thread/goal" not in diagnostic_bytes


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    codex_home = tmp_path / "codex-home"
    root = codex_home / "codex-must-work"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    _ = transcript.write_text("", encoding="utf-8")
    return root, transcript
