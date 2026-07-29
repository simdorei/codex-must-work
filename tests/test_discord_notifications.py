from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, cast, final

import pytest

from scripts.discord_notifications import (
    CMW_DISCORD_WEBHOOK_ENV,
    DiscordNotificationSink,
    notification_sink_from_configuration,
    notification_sink_from_environment,
)
from scripts.discord_webhook import (
    DiscordWebhookClient,
    DiscordWebhookError,
    parse_discord_webhook_url,
)
from scripts.notification_config import NotificationConfigStore
from scripts.notifications import (
    LifecycleNotification,
    NotificationDeliveryError,
    NotificationKind,
    NotificationSubject,
    NotificationSubjectKind,
    NullNotificationSink,
)
from scripts.private_root import ensure_private_root
from scripts.state import StateDocument, save_state
from scripts.thread_title import ThreadTitleResolver

if TYPE_CHECKING:
    from pathlib import Path


@final
class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"id":"message-123"}') -> None:
        self.status = status
        self._body = body

    def read(self, amount: int) -> bytes:
        assert amount == 65_537
        return self._body


@final
class _FakeConnection:
    def __init__(self, response: _FakeResponse | None = None) -> None:
        self.response = response or _FakeResponse()
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> _FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def _event(kind: NotificationKind) -> LifecycleNotification:
    return LifecycleNotification(
        event_id="a" * 64,
        session_id="thread-1",
        kind=kind,
        subject=NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
        elapsed_ms=90_000 if kind is NotificationKind.BOTTLENECK_SUSPECTED else None,
    )


def test_webhook_client_posts_waiting_payload_without_mentions() -> None:
    connection = _FakeConnection()
    factory_calls: list[tuple[str, int, float]] = []

    def factory(host: str, port: int, timeout: float) -> _FakeConnection:
        factory_calls.append((host, port, timeout))
        return connection

    client = DiscordWebhookClient(
        "https://discord.com/api/webhooks/123456789/token-value",
        connection_factory=factory,
    )

    message_id = client.send("CMW status @everyone")

    assert message_id == "message-123"
    assert factory_calls == [("discord.com", 443, 5.0)]
    assert connection.closed is True
    method, path, encoded, headers = connection.requests[0]
    assert method == "POST"
    assert path == "/api/webhooks/123456789/token-value?wait=true"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    decoded = cast("object", json.loads(encoded))
    assert isinstance(decoded, dict)
    payload = cast("dict[str, object]", decoded)
    assert payload == {
        "allowed_mentions": {"parse": []},
        "content": "CMW status @everyone",
    }


def test_webhook_client_rejects_content_over_discord_limit_before_connecting() -> None:
    connections = 0

    def factory(_host: str, _port: int, _timeout: float) -> _FakeConnection:
        nonlocal connections
        connections += 1
        return _FakeConnection()

    client = DiscordWebhookClient(
        "https://discord.com/api/webhooks/123456789/token-value",
        connection_factory=factory,
    )

    with pytest.raises(DiscordWebhookError, match="discord_content_too_large"):
        _ = client.send("x" * 2_001)

    assert connections == 0


@pytest.mark.parametrize(
    "webhook_url",
    [
        "http://discord.com/api/webhooks/123/token",
        "https://example.com/api/webhooks/123/token",
        "https://discord.com/api/webhooks/123/token?redirect=1",
        "https://discord.com/api/webhooks/not-a-number/token",
    ],
)
def test_webhook_client_rejects_unsafe_urls_without_echoing_secret(webhook_url: str) -> None:
    with pytest.raises(DiscordWebhookError) as raised:
        _ = DiscordWebhookClient(webhook_url)

    assert "token" not in str(raised.value)
    assert webhook_url not in str(raised.value)


def test_webhook_client_surfaces_safe_http_failure() -> None:
    connection = _FakeConnection(_FakeResponse(status=401, body=b"private response"))
    client = DiscordWebhookClient(
        "https://discord.com/api/webhooks/123456789/token-value",
        connection_factory=lambda _host, _port, _timeout: connection,
    )

    with pytest.raises(DiscordWebhookError, match="discord_http_401") as raised:
        _ = client.send("hello")

    assert "private response" not in str(raised.value)


def test_environment_keeps_discord_disabled_until_explicitly_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CMW_DISCORD_WEBHOOK_ENV, raising=False)

    sink = notification_sink_from_environment()

    assert isinstance(sink, NullNotificationSink)


def test_title_resolver_reads_codex_database_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "state_1.sqlite"
    with sqlite3.connect(database) as connection:
        _ = connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
        _ = connection.execute(
            "INSERT INTO threads (id, title) VALUES (?, ?)",
            ("thread-1", "CMW webhook QA"),
        )

    resolver = ThreadTitleResolver(state_database=database)

    assert resolver.resolve("thread-1") == "CMW webhook QA"
    assert resolver.resolve("missing-thread") is None


def test_sink_names_specific_subagent_from_local_codex_metadata() -> None:
    sent: list[str] = []

    class _Client:
        def send(self, content: str) -> str | None:
            sent.append(content)
            return "message-id"

    event = LifecycleNotification(
        event_id="b" * 64,
        session_id="thread-1",
        kind=NotificationKind.BOTTLENECK_SUSPECTED,
        subject=NotificationSubject(NotificationSubjectKind.SUBAGENT, target_id="child-1"),
        elapsed_ms=90_000,
    )
    sink = DiscordNotificationSink(
        _Client(),
        lambda _session_id: "CMW webhook QA",
        lambda _subject: "Tesla (explorer)",
    )

    sink.notify(event)

    assert "Tesla (explorer)" in sent[0]


def test_configured_sink_observes_setup_without_daemon_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    data_root = tmp_path / "plugin-data"

    class _Client:
        def __init__(self, _webhook_url: str) -> None:
            pass

        def send(self, content: str) -> str | None:
            sent.append(content)
            return "message-id"

    monkeypatch.delenv(CMW_DISCORD_WEBHOOK_ENV, raising=False)
    monkeypatch.setattr("scripts.discord_notifications.DiscordWebhookClient", _Client)
    sink = notification_sink_from_configuration(data_root)

    sink.notify(_event(NotificationKind.BOTTLENECK_SUSPECTED))
    assert sent == []

    NotificationConfigStore(data_root).save_discord_webhook(
        parse_discord_webhook_url("https://discord.com/api/webhooks/123456789/test-token-value")
    )
    sink.notify(_event(NotificationKind.BOTTLENECK_SUSPECTED))

    assert len(sent) == 1
    assert "메인 에이전트" in sent[0]


def test_configured_sink_reports_malformed_private_state_as_delivery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "plugin-data"
    monkeypatch.delenv(CMW_DISCORD_WEBHOOK_ENV, raising=False)
    ensure_private_root(data_root)
    save_state(
        data_root,
        data_root / "notifications.json",
        StateDocument(values={"discord_webhook_url": "not-a-webhook"}),
    )
    sink = notification_sink_from_configuration(data_root)

    with pytest.raises(NotificationDeliveryError, match="discord_config_invalid"):
        sink.notify(_event(NotificationKind.BOTTLENECK_SUSPECTED))
