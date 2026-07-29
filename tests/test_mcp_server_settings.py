from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from scripts.state_io import JsonValue

from scripts.durations import Milliseconds
from scripts.state_io import StateLockTimeoutError
from scripts.threshold_settings import ThresholdSettingsSnapshot, ThresholdSettingsStore
from tests.mcp_server_test_support import (
    FakeDaemon,
    capability,
    ready_server,
    request,
    success_result,
)


def test_settings_show_returns_five_and_ten_minute_defaults(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    store = ThresholdSettingsStore(tmp_path / "state")
    server = ready_server(daemon, threshold_settings=store)
    arguments: dict[str, JsonValue] = {
        "session_id": "settings-session",
        "control_capability": capability("settings-session"),
        "action": "show",
    }

    response = server.handle_line(
        request(31, "tools/call", {"name": "cmw.settings", "arguments": arguments})
    )

    result = success_result(response)
    assert result["structuredContent"] == {
        "status": "ready",
        "mode": "default",
        "warning_after_ms": 300_000,
        "critical_after_ms": 600_000,
    }


def test_settings_custom_persists_exact_thresholds(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    root = tmp_path / "state"
    server = ready_server(daemon, threshold_settings=ThresholdSettingsStore(root))
    arguments: dict[str, JsonValue] = {
        "session_id": "settings-session",
        "control_capability": capability("settings-session"),
        "action": "custom",
        "warning_after_ms": 420_000,
        "critical_after_ms": 900_000,
    }

    response = server.handle_line(
        request(32, "tools/call", {"name": "cmw.settings", "arguments": arguments})
    )

    result = success_result(response)
    assert result["structuredContent"] == {
        "status": "ready",
        "mode": "custom",
        "warning_after_ms": 420_000,
        "critical_after_ms": 900_000,
    }
    loaded = ThresholdSettingsStore(root).load()
    assert loaded.warning_after_ms == Milliseconds(420_000)
    assert loaded.critical_after_ms == Milliseconds(900_000)


def test_settings_recommended_uses_local_history_values(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    store = ThresholdSettingsStore(
        tmp_path / "state",
        recommendation=lambda: (Milliseconds(180_000), Milliseconds(480_000)),
    )
    server = ready_server(daemon, threshold_settings=store)
    arguments: dict[str, JsonValue] = {
        "session_id": "settings-session",
        "control_capability": capability("settings-session"),
        "action": "recommended",
    }

    response = server.handle_line(
        request(33, "tools/call", {"name": "cmw.settings", "arguments": arguments})
    )

    result = success_result(response)
    assert result["structuredContent"] == {
        "status": "ready",
        "mode": "recommended",
        "warning_after_ms": 180_000,
        "critical_after_ms": 480_000,
    }


def test_settings_custom_requires_both_ordered_thresholds(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    server = ready_server(
        daemon,
        threshold_settings=ThresholdSettingsStore(tmp_path / "state"),
    )
    arguments: dict[str, JsonValue] = {
        "session_id": "settings-session",
        "control_capability": capability("settings-session"),
        "action": "custom",
        "warning_after_ms": 600_000,
        "critical_after_ms": 300_000,
    }

    response = server.handle_line(
        request(34, "tools/call", {"name": "cmw.settings", "arguments": arguments})
    )

    result = success_result(response)
    assert result["isError"] is True
    assert result["structuredContent"] == {"error": "threshold_order_invalid"}


def test_settings_state_failure_returns_stable_error_without_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ThresholdSettingsStore(tmp_path / "state")
    private_path = tmp_path / "private" / "threshold-settings.json"

    def fail_load() -> ThresholdSettingsSnapshot:
        raise StateLockTimeoutError(private_path)

    monkeypatch.setattr(store, "load", fail_load)
    server = ready_server(FakeDaemon(), threshold_settings=store)
    arguments: dict[str, JsonValue] = {
        "session_id": "settings-session",
        "control_capability": capability("settings-session"),
        "action": "show",
    }

    response = server.handle_line(
        request(35, "tools/call", {"name": "cmw.settings", "arguments": arguments})
    )

    result = success_result(response)
    assert result["structuredContent"] == {"error": "monitoring_state_unavailable"}
    assert str(private_path) not in json.dumps(result)
