from __future__ import annotations

import http.client
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlencode, urlsplit

from scripts.discord_webhook import DiscordWebhookError
from scripts.notification_config import NotificationConfigStore
from scripts.notification_setup import NotificationSetupCoordinator
from scripts.notification_setup_page import render_setup_page

if TYPE_CHECKING:
    from pathlib import Path

_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


class _Client(Protocol):
    def send(self, content: str) -> str | None: ...


@dataclass
class _RecordingClient:
    messages: list[str]
    failure: RuntimeError | None = None

    def send(self, content: str) -> str | None:
        if self.failure is not None:
            raise self.failure
        self.messages.append(content)
        return "message-id"


def _webhook() -> str:
    return "https://discord.com/api/" + "webhooks/123456789/test-token-value"


def _request(
    url: str,
    method: str,
    *,
    body: str | None = None,
    origin: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    headers = {"Host": parsed.netloc}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body.encode("utf-8")))
    if origin is not None:
        headers["Origin"] = origin
    connection.request(method, parsed.path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, payload


def _csrf(html: bytes) -> str:
    match = _CSRF.search(html.decode("utf-8"))
    assert match is not None
    return match.group(1)


def test_setup_binds_loopback_and_never_places_webhook_in_page_or_url(tmp_path: Path) -> None:
    client = _RecordingClient([])
    coordinator = NotificationSetupCoordinator(
        tmp_path / "plugin-data",
        client_factory=lambda _url: cast("_Client", client),
    )
    launch = coordinator.start()
    try:
        parsed = urlsplit(launch.setup_url)
        status, headers, page = _request(launch.setup_url, "GET")

        assert parsed.hostname == "127.0.0.1"
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert "default-src 'none'" in headers["content-security-policy"]
        assert _webhook() not in launch.setup_url
        assert _webhook().encode() not in page
        assert b"Discord" in page
    finally:
        coordinator.close()


def test_repeated_setup_reuses_one_listener_and_one_url(tmp_path: Path) -> None:
    coordinator = NotificationSetupCoordinator(
        tmp_path / "plugin-data",
        client_factory=lambda _url: cast("_Client", _RecordingClient([])),
    )
    try:
        first = coordinator.start()
        second = coordinator.start()

        assert second == first
        assert coordinator.is_active() is True
    finally:
        coordinator.close()


def test_setup_tests_saves_and_closes_without_returning_secret(tmp_path: Path) -> None:
    client = _RecordingClient([])
    data_root = tmp_path / "plugin-data"
    coordinator = NotificationSetupCoordinator(
        data_root,
        client_factory=lambda _url: cast("_Client", client),
    )
    launch = coordinator.start()
    status, _headers, page = _request(launch.setup_url, "GET")
    assert status == 200
    csrf = _csrf(page)
    origin = launch.setup_url.rsplit("/", 1)[0].rsplit("/", 1)[0]
    body = urlencode({"csrf_token": csrf, "webhook_url": _webhook()})

    status, _headers, response = _request(
        launch.setup_url,
        "POST",
        body=body,
        origin=origin,
    )

    assert status == 200
    decoded = cast("dict[str, str]", json.loads(response))
    assert decoded == {"status": "connected"}
    assert _webhook().encode() not in response
    assert client.messages == ["CMW Discord 알림 연결 테스트가 완료되었습니다."]
    assert str(NotificationConfigStore(data_root).load_discord_webhook()) == _webhook()
    deadline = time.monotonic() + 2
    while coordinator.is_active() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert coordinator.is_active() is False
    coordinator.close()


def test_setup_rejects_cross_origin_and_keeps_retryable_server_active(tmp_path: Path) -> None:
    data_root = tmp_path / "plugin-data"
    coordinator = NotificationSetupCoordinator(
        data_root,
        client_factory=lambda _url: cast("_Client", _RecordingClient([])),
    )
    launch = coordinator.start()
    try:
        _, _, page = _request(launch.setup_url, "GET")
        body = urlencode({"csrf_token": _csrf(page), "webhook_url": _webhook()})

        status, _headers, response = _request(
            launch.setup_url,
            "POST",
            body=body,
            origin="https://attacker.example",
        )

        assert status == 403
        assert _webhook().encode() not in response
        assert NotificationConfigStore(data_root).is_discord_configured() is False
        assert coordinator.is_active() is True
    finally:
        coordinator.close()


def test_setup_rejects_oversized_form_without_saving(tmp_path: Path) -> None:
    data_root = tmp_path / "plugin-data"
    coordinator = NotificationSetupCoordinator(
        data_root,
        client_factory=lambda _url: cast("_Client", _RecordingClient([])),
    )
    launch = coordinator.start()
    try:
        origin = launch.setup_url.rsplit("/", 1)[0].rsplit("/", 1)[0]

        status, _headers, response = _request(
            launch.setup_url,
            "POST",
            body="x" * 4_097,
            origin=origin,
        )

        assert status == 413
        assert _webhook().encode() not in response
        assert NotificationConfigStore(data_root).is_discord_configured() is False
        assert coordinator.is_active() is True
    finally:
        coordinator.close()


def test_setup_failure_is_safe_and_does_not_save(tmp_path: Path) -> None:
    data_root = tmp_path / "plugin-data"
    client = _RecordingClient([], DiscordWebhookError("private remote details"))
    coordinator = NotificationSetupCoordinator(
        data_root,
        client_factory=lambda _url: cast("_Client", client),
    )
    launch = coordinator.start()
    try:
        _, _, page = _request(launch.setup_url, "GET")
        origin = launch.setup_url.rsplit("/", 1)[0].rsplit("/", 1)[0]
        body = urlencode({"csrf_token": _csrf(page), "webhook_url": _webhook()})

        status, _headers, response = _request(
            launch.setup_url,
            "POST",
            body=body,
            origin=origin,
        )

        assert status == 502
        assert b"private remote details" not in response
        assert _webhook().encode() not in response
        assert NotificationConfigStore(data_root).is_discord_configured() is False
        assert coordinator.is_active() is True
    finally:
        coordinator.close()


def test_setup_page_uses_reusable_shape_and_typography_tokens() -> None:
    html = render_setup_page("csrf", "nonce").decode("utf-8")

    for token, minimum_uses in (
        ("--font-sans", 3),
        ("--text-size-body", 3),
        ("--text-size-small", 3),
        ("--radius-control", 3),
        ("--control-height", 2),
        ("--tracking-micro", 1),
        ("--tracking-heading", 1),
    ):
        assert html.count(f"var({token})") >= minimum_uses
    assert "<header>" in html
    assert "</header>" in html


def test_setup_page_preserves_korean_words_and_separates_live_feedback() -> None:
    html = render_setup_page("csrf", "nonce").decode("utf-8")

    assert "word-break: keep-all" in html
    assert ".result:not(:empty)" in html
    assert 'field.removeAttribute("aria-invalid")' in html
    assert "Codex 앱을 한 번 재시작하는 것을 권장합니다" in html
