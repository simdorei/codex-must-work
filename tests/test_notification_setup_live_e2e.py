from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.notification_setup_live_e2e import (
    LiveSetupError,
    load_user_supplied_webhook,
)

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.state_io import JsonValue


def _webhook(webhook_id: str, token: str) -> str:
    return f"https://discord.com/api/webhooks/{webhook_id}/{token}"


def _write_rollout(tmp_path: Path, thread_id: str, rows: list[JsonValue]) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-{thread_id}.jsonl"
    _ = rollout.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_load_webhook_uses_only_canonical_user_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "thread-user-only"
    expected = _webhook("123456789", "real-user-token")
    later_fake = _webhook("987654321", "later-tool-token")
    _write_rollout(
        tmp_path,
        thread_id,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": expected}],
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": expected},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "arguments": json.dumps({"webhook": later_fake}),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": f"QA used {later_fake}",
                },
            },
        ],
    )
    monkeypatch.setattr("tests.notification_setup_live_e2e.Path.home", lambda: tmp_path)

    assert str(load_user_supplied_webhook(thread_id)) == expected


def test_load_webhook_chooses_latest_user_supplied_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "thread-latest-user"
    old = _webhook("123456789", "old-user-token")
    expected = _webhook("123456789", "new-user-token")
    _write_rollout(
        tmp_path,
        thread_id,
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": old}},
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": expected},
            },
        ],
    )
    monkeypatch.setattr("tests.notification_setup_live_e2e.Path.home", lambda: tmp_path)

    assert str(load_user_supplied_webhook(thread_id)) == expected


def test_load_webhook_rejects_malformed_rollout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = "thread-malformed"
    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-{thread_id}.jsonl"
    _ = rollout.write_text("{not-json}\n", encoding="utf-8")
    monkeypatch.setattr("tests.notification_setup_live_e2e.Path.home", lambda: tmp_path)

    with pytest.raises(LiveSetupError, match="rollout_invalid"):
        _ = load_user_supplied_webhook(thread_id)
