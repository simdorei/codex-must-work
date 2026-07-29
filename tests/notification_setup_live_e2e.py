# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run one secret-safe local-setup and Discord delivery E2E."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
from pathlib import Path
from typing import Never, cast
from urllib.parse import urlencode, urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.discord_notifications import DiscordNotificationSink
from scripts.discord_webhook import (
    DiscordWebhookClient,
    DiscordWebhookError,
    DiscordWebhookUrl,
    parse_discord_webhook_url,
)
from scripts.notification_config import NotificationConfigStore
from scripts.notification_setup import NotificationSetupCoordinator
from scripts.notifications import (
    LifecycleNotification,
    NotificationKind,
    NotificationSubject,
    NotificationSubjectKind,
)

_WEBHOOK = re.compile(r"https://discord\.com/api/webhooks/[1-9][0-9]{5,24}/[^\\\s\"?#]{6,256}")
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
_MAX_ROLLOUT_BYTES = 64 * 1024 * 1024


class LiveSetupError(RuntimeError):
    """Expose only a public-safe live QA failure code."""


class _Args(argparse.Namespace):
    thread_id: str
    plugin_data: Path

    def __init__(self) -> None:
        """Initialize typed CLI destinations."""
        super().__init__()
        self.thread_id = ""
        self.plugin_data = Path()


def main(argv: list[str] | None = None) -> int:
    """Configure the installed data root and send target-specific QA alerts."""
    parser = argparse.ArgumentParser(prog="notification-setup-live-e2e")
    _ = parser.add_argument("--thread-id", required=True)
    _ = parser.add_argument("--plugin-data", type=Path, required=True)
    arguments = parser.parse_args(argv, namespace=_Args())
    try:
        webhook = load_user_supplied_webhook(arguments.thread_id)
        _submit_setup(arguments.plugin_data.resolve(), webhook)
        _send_target_samples(webhook)
    except (DiscordWebhookError, LiveSetupError, OSError) as error:
        _ = sys.stderr.write(
            json.dumps({"passed": False, "reason": str(error)}, separators=(",", ":")) + "\n"
        )
        return 1
    _ = sys.stdout.write(
        json.dumps(
            {
                "passed": True,
                "configured": True,
                "discord_messages_acknowledged": 4,
                "secret_disclosed": False,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


def load_user_supplied_webhook(thread_id: str) -> DiscordWebhookUrl:
    sessions = Path.home() / ".codex" / "sessions"
    candidates = tuple(sessions.rglob(f"*{thread_id}*.jsonl"))
    if not candidates:
        _fail("rollout_not_found")
    rollout = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    if rollout.stat().st_size > _MAX_ROLLOUT_BYTES:
        _fail("rollout_too_large")
    matches: list[re.Match[str]] = []
    with rollout.open(encoding="utf-8") as source:
        for line in source:
            message = _canonical_user_message(line)
            if message is not None:
                matches.extend(_WEBHOOK.finditer(message))
    if not matches:
        _fail("webhook_not_found")
    return parse_discord_webhook_url(matches[-1].group(0))


def _canonical_user_message(line: str) -> str | None:
    if not line.strip():
        return None
    try:
        decoded = cast("object", json.loads(line))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("rollout_invalid")
    if not isinstance(decoded, dict):
        _fail("rollout_invalid")
    row = cast("dict[str, object]", decoded)
    if row.get("type") != "event_msg":
        return None
    raw_payload = row.get("payload")
    if not isinstance(raw_payload, dict):
        return None
    payload = cast("dict[str, object]", raw_payload)
    message = payload.get("message")
    if payload.get("type") != "user_message" or not isinstance(message, str):
        return None
    return message


def _submit_setup(plugin_data: Path, webhook: DiscordWebhookUrl) -> None:
    coordinator = NotificationSetupCoordinator(plugin_data)
    launch = coordinator.start()
    try:
        status, page = _request(launch.setup_url, "GET")
        if status != 200:
            _fail("setup_page_failed")
        csrf_match = _CSRF.search(page.decode("utf-8"))
        if csrf_match is None:
            _fail("setup_csrf_missing")
        origin = launch.setup_url.rsplit("/", 2)[0]
        body = urlencode(
            {
                "csrf_token": csrf_match.group(1),
                "webhook_url": str(webhook),
            }
        )
        status, response = _request(launch.setup_url, "POST", body=body, origin=origin)
        if status != 200:
            _fail(f"setup_submit_http_{status}")
        if response != b'{"status":"connected"}':
            _fail("setup_submit_response_invalid")
        deadline = time.monotonic() + 2
        while coordinator.is_active() and time.monotonic() < deadline:
            time.sleep(0.01)
        configured = NotificationConfigStore(plugin_data).load_discord_webhook()
        if configured != webhook or coordinator.is_active():
            _fail("setup_persistence_failed")
    finally:
        coordinator.close()


def _request(
    url: str,
    method: str,
    *,
    body: str | None = None,
    origin: str | None = None,
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if parsed.hostname != "127.0.0.1" or parsed.port is None:
        _fail("setup_origin_invalid")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    headers = {"Host": parsed.netloc}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body.encode("utf-8")))
    if origin is not None:
        headers["Origin"] = origin
    connection.request(method, parsed.path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, payload


def _send_target_samples(webhook: DiscordWebhookUrl) -> None:
    sink = DiscordNotificationSink(
        DiscordWebhookClient(webhook),
        lambda _session_id: "CMW 알림 QA",
    )
    sink.notify(
        LifecycleNotification(
            "a" * 64,
            "live-qa",
            NotificationKind.BOTTLENECK_SUSPECTED,
            NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
            90_000,
        )
    )
    sink.notify(
        LifecycleNotification(
            "b" * 64,
            "live-qa",
            NotificationKind.BOTTLENECK_CRITICAL,
            NotificationSubject(NotificationSubjectKind.MAIN_AGENT),
            600_000,
        )
    )
    sink.notify(
        LifecycleNotification(
            "c" * 64,
            "live-qa",
            NotificationKind.PROGRESS_RECOVERED,
            NotificationSubject(NotificationSubjectKind.SUBAGENT, "qa-subagent"),
        )
    )


def _fail(reason: str) -> Never:
    raise LiveSetupError(reason)


if __name__ == "__main__":
    raise SystemExit(main())
