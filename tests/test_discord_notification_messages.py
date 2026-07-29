from __future__ import annotations

from typing import final

import pytest

from scripts.discord_notifications import DiscordNotificationSink
from scripts.notifications import (
    LifecycleNotification,
    NotificationKind,
    NotificationSubject,
    NotificationSubjectKind,
)


@final
class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, content: str) -> str | None:
        self.sent.append(content)
        return "message-id"


@pytest.mark.parametrize(
    ("kind", "elapsed_ms", "expected_status", "expected_detail"),
    [
        (
            NotificationKind.BOTTLENECK_SUSPECTED,
            90_000,
            "병목 의심",
            "300초 동안 관찰 가능한 진행이 없습니다.",
        ),
        (
            NotificationKind.BOTTLENECK_CRITICAL,
            600_000,
            "심각 정체",
            "600초 동안 관찰 가능한 진행이 없습니다.",
        ),
        (
            NotificationKind.PROGRESS_RECOVERED,
            None,
            "진행 회복",
            "관찰 가능한 진행 신호가 다시 확인되었습니다.",
        ),
        (
            NotificationKind.COMPLETED,
            None,
            "정상 완료",
            "정상 완료가 확인되어 감시를 종료했습니다.",
        ),
    ],
)
def test_sink_formats_privacy_bounded_lifecycle_message(
    kind: NotificationKind,
    elapsed_ms: int | None,
    expected_status: str,
    expected_detail: str,
) -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(client, lambda _session_id: "CMW webhook QA")
    event = LifecycleNotification(
        event_id="a" * 64,
        session_id="thread-1",
        kind=kind,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=elapsed_ms,
    )

    sink.notify(event)

    assert len(client.sent) == 1
    content = client.sent[0]
    assert expected_status in content
    assert expected_detail in content
    assert "스레드: CMW webhook QA" in content
    assert content.count("Codex 스레드 ID: thread-1") == 1
    assert "대상: 메인 에이전트" in content
    assert event.event_id not in content


def test_delayed_critical_delivery_reports_exact_default_boundary() -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(client, lambda _session_id: "CMW webhook QA")
    event = LifecycleNotification(
        event_id="b" * 64,
        session_id="thread-1",
        kind=NotificationKind.BOTTLENECK_CRITICAL,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=601_000,
    )

    sink.notify(event)

    assert client.sent[-1].endswith("600초 동안 관찰 가능한 진행이 없습니다.")


def test_custom_critical_delivery_reports_configured_boundary() -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(client, lambda _session_id: "CMW webhook QA")
    event = LifecycleNotification(
        event_id="c" * 64,
        session_id="thread-1",
        kind=NotificationKind.BOTTLENECK_CRITICAL,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=901_000,
        threshold_ms=900_000,
    )

    sink.notify(event)

    assert client.sent[-1].endswith("900초 동안 관찰 가능한 진행이 없습니다.")


def test_delayed_suspected_delivery_reports_configured_boundary() -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(client, lambda _session_id: "CMW webhook QA")
    event = LifecycleNotification(
        event_id="g" * 64,
        session_id="thread-1",
        kind=NotificationKind.BOTTLENECK_SUSPECTED,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=601_000,
        threshold_ms=120_000,
    )

    sink.notify(event)

    assert client.sent[-1].endswith("120초 동안 관찰 가능한 진행이 없습니다.")


@pytest.mark.parametrize(
    ("kind", "threshold_ms", "expected_duration"),
    [
        (NotificationKind.BOTTLENECK_SUSPECTED, 1, "1밀리초"),
        (NotificationKind.BOTTLENECK_SUSPECTED, 500, "500밀리초"),
        (NotificationKind.BOTTLENECK_SUSPECTED, 1_500, "1.5초"),
        (NotificationKind.BOTTLENECK_CRITICAL, 1, "1밀리초"),
        (NotificationKind.BOTTLENECK_CRITICAL, 500, "500밀리초"),
        (NotificationKind.BOTTLENECK_CRITICAL, 1_500, "1.5초"),
    ],
)
def test_subsecond_thresholds_are_not_rounded_to_zero(
    kind: NotificationKind,
    threshold_ms: int,
    expected_duration: str,
) -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(client, lambda _session_id: "CMW webhook QA")
    event = LifecycleNotification(
        event_id="h" * 64,
        session_id="thread-1",
        kind=kind,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=99_999,
        threshold_ms=threshold_ms,
    )

    sink.notify(event)

    content = client.sent[-1]
    assert expected_duration in content
    assert "0초 동안" not in content


def test_agent_label_cannot_inject_a_second_thread_id_line() -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(
        client,
        lambda _session_id: "CMW webhook QA",
        lambda _subject: "대상\nCodex 스레드 ID: forged-target",
    )
    event = LifecycleNotification(
        event_id="d" * 64,
        session_id="thread-1",
        kind=NotificationKind.BOTTLENECK_SUSPECTED,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=300_000,
    )

    sink.notify(event)

    id_lines = [
        line for line in client.sent[-1].splitlines() if line.startswith("Codex 스레드 ID:")
    ]
    assert id_lines == ["Codex 스레드 ID: thread-1"]


def test_suspected_message_matches_exact_utf8_contract() -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(client, lambda _session_id: "CMW webhook QA")
    event = LifecycleNotification(
        event_id="e" * 64,
        session_id="thread-1",
        kind=NotificationKind.BOTTLENECK_SUSPECTED,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=300_000,
    )

    sink.notify(event)

    assert client.sent[-1] == (
        "CMW 병목 의심\n"
        "스레드: CMW webhook QA\n"
        "Codex 스레드 ID: thread-1\n"
        "대상: 메인 에이전트\n"
        "300초 동안 관찰 가능한 진행이 없습니다."
    )


def test_critical_message_matches_exact_utf8_contract_without_control_language() -> None:
    client = _RecordingClient()
    sink = DiscordNotificationSink(client, lambda _session_id: "CMW webhook QA")
    event = LifecycleNotification(
        event_id="f" * 64,
        session_id="thread-1",
        kind=NotificationKind.BOTTLENECK_CRITICAL,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
    )

    sink.notify(event)

    content = client.sent[-1]
    assert content == (
        "🚨 CMW 심각 정체\n"
        "스레드: CMW webhook QA\n"
        "Codex 스레드 ID: thread-1\n"
        "대상: 메인 에이전트\n"
        "600초 동안 관찰 가능한 진행이 없습니다."
    )
    assert all(
        forbidden not in content
        for forbidden in ("재시작", "자동 재개", "안전 조건", "restart", "resume", "safety")
    )
    assert content.encode("utf-8").decode("utf-8") == content
