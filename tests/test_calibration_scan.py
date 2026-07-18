from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from scripts.calibration import CalibrationRecommendation
from scripts.calibration_scan import ScanLimits, scan_history

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.event_source import JsonValue

_NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


def _record(at: datetime, record_type: str, payload: dict[str, JsonValue]) -> str:
    timestamp = at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return json.dumps(
        {"timestamp": timestamp, "type": record_type, "payload": payload},
        separators=(",", ":"),
    )


def _write_history(path: Path, *, private_body: str = "") -> None:
    at = _NOW - timedelta(days=1)
    records = [_record(at, "event_msg", {"type": "task_started", "turn_id": "turn-1"})]
    for index in range(10):
        at += timedelta(minutes=1)
        records.append(
            _record(
                at,
                "response_item",
                {"type": "reasoning", "id": f"item-{index}", "content": private_body},
            )
        )
    at += timedelta(minutes=1)
    records.append(_record(at, "response_item", {"type": "function_call", "call_id": "call-1"}))
    at += timedelta(hours=2)
    records.append(
        _record(at, "response_item", {"type": "function_call_output", "call_id": "call-1"})
    )
    for index in range(9):
        at += timedelta(minutes=1)
        records.append(
            _record(at, "response_item", {"type": "agent_message", "id": f"tail-{index}"})
        )
    at += timedelta(days=1)
    records.append(_record(at, "event_msg", {"type": "task_complete", "turn_id": "turn-1"}))
    path.parent.mkdir(parents=True)
    _ = path.write_text("\n".join(records) + "\n", encoding="utf-8")
    timestamp = (_NOW - timedelta(days=1)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_scan_history_excludes_tool_wait_and_turn_terminal_gap(tmp_path: Path) -> None:
    # Given: twenty one-minute progress gaps around a two-hour tool wait.
    private_body = "PRIVATE-CONTENT-MUST-NOT-SURVIVE"
    _write_history(tmp_path / "sessions" / "rollout.jsonl", private_body=private_body)

    # When: recent local history is scanned.
    result = scan_history(tmp_path, _NOW)

    # Then: the wait and terminal gap do not inflate the recommendation.
    assert isinstance(result, CalibrationRecommendation)
    assert result.sample_count == 20
    assert result.warning_after_ms == 60_000
    assert result.restart_after_ms == 120_000
    assert private_body not in repr(result)


def test_scan_history_honors_recent_file_limit(tmp_path: Path) -> None:
    # Given: two usable histories but a one-file scan limit.
    older = tmp_path / "sessions" / "older.jsonl"
    newer = tmp_path / "archived_sessions" / "newer.jsonl"
    _write_history(older)
    _write_history(newer)
    older_time = (_NOW - timedelta(days=2)).timestamp()
    newer_time = (_NOW - timedelta(hours=1)).timestamp()
    os.utime(older, (older_time, older_time))
    os.utime(newer, (newer_time, newer_time))
    limits = ScanLimits(max_files=1, max_total_bytes=1024 * 1024, max_file_bytes=1024 * 1024)

    # When: the bounded scan runs.
    result = scan_history(tmp_path, _NOW, limits)

    # Then: only the newest file contributes its twenty gaps.
    assert isinstance(result, CalibrationRecommendation)
    assert result.sample_count == 20
