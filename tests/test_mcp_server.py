from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, final

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from scripts.mcp_protocol import JsonObject, JsonRpcResponse
    from scripts.state_io import JsonValue

from scripts.control_capability import derive_control_capability
from scripts.daemon_models import (
    DaemonServiceError,
    SessionId,
    SessionRequest,
    StartRequest,
    ToolResult,
)
from scripts.mcp_server import McpServer, configure_plugin_data
from scripts.notification_setup import NotificationSetupLaunch


@final
class FakeDaemon:
    def __init__(self, *, failure: DaemonServiceError | None = None) -> None:
        self.failure = failure
        self.started: StartRequest | None = None
        self.session_request: SessionRequest | None = None
        self.closed = False

    def start(self, request: StartRequest) -> ToolResult:
        self.started = request
        return self._result(request.session_id, "started")

    def stop(self, request: SessionRequest) -> ToolResult:
        self.session_request = request
        return self._result(request.session_id, "stopped")

    def status(self, request: SessionRequest) -> ToolResult:
        self.session_request = request
        if self.failure is not None:
            raise self.failure
        return self._result(request.session_id, "active")

    def complete(self, request: SessionRequest) -> ToolResult:
        self.session_request = request
        return self._result(request.session_id, "completed")

    def close(self) -> None:
        self.closed = True

    @staticmethod
    def _result(session_id: SessionId, status: str) -> ToolResult:
        return ToolResult(session_id=session_id, status=status)


def test_initialize_negotiates_supported_protocol() -> None:
    # Given
    server = McpServer(FakeDaemon(), _test_key())
    request = _request(
        1,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
    )

    # When
    response = server.handle_line(request)

    # Then
    assert _success_result(response)["protocolVersion"] == "2025-06-18"


def test_plugin_data_defaults_to_host_injected_environment(tmp_path: Path) -> None:
    environment = {"PLUGIN_DATA": str(tmp_path / "plugin-data")}

    resolved = configure_plugin_data([], cwd=tmp_path, environ=environment)

    assert resolved == (tmp_path / "plugin-data").resolve()
    assert environment["PLUGIN_DATA"] == str(resolved)


def test_plugin_data_cli_argument_overrides_environment(tmp_path: Path) -> None:
    environment = {"PLUGIN_DATA": str(tmp_path / "from-host")}

    resolved = configure_plugin_data(
        ["--plugin-data", "from-cli"],
        cwd=tmp_path,
        environ=environment,
    )

    assert resolved == (tmp_path / "from-cli").resolve()
    assert environment["PLUGIN_DATA"] == str(resolved)


def test_tools_list_exposes_only_cmw_control_tools_after_initialized() -> None:
    # Given
    server = _ready_server(FakeDaemon())

    # When
    response = server.handle_line(_request(2, "tools/list", {}))

    # Then
    tools = _success_result(response).get("tools")
    assert isinstance(tools, list)
    names: set[str] = set()
    for tool in tools:
        assert isinstance(tool, dict)
        name = tool.get("name")
        assert isinstance(name, str)
        names.add(name)
    assert names == {
        "cmw.start",
        "cmw.stop",
        "cmw.status",
        "cmw.complete",
    }
    for tool in tools:
        assert isinstance(tool, dict)
        schema = tool.get("inputSchema")
        assert isinstance(schema, dict)
        required = schema.get("required")
        assert isinstance(required, list)
        assert "control_capability" in required


@final
class _SetupLauncher:
    def __init__(self) -> None:
        self.calls = 0

    def start(self) -> NotificationSetupLaunch:
        self.calls += 1
        return NotificationSetupLaunch(
            setup_url="http://127.0.0.1:45123/setup/one-time",
            expires_in_seconds=300,
        )


def test_notification_setup_tool_exposes_only_local_short_lived_url() -> None:
    launcher = _SetupLauncher()
    server = McpServer(
        FakeDaemon(),
        _test_key(),
        notification_setup=launcher,
    )
    _ = server.handle_line(_request(1, "initialize", {"protocolVersion": "2025-11-25"}))
    assert server.handle_line(_notification("notifications/initialized")) is None

    listed = _success_result(server.handle_line(_request(2, "tools/list", {})))
    tools = listed["tools"]
    assert isinstance(tools, list)
    names: set[str] = set()
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                names.add(name)
    assert "cmw.notifications.setup" in names

    called = _success_result(
        server.handle_line(
            _request(
                3,
                "tools/call",
                {"name": "cmw.notifications.setup", "arguments": {}},
            )
        )
    )

    assert launcher.calls == 1
    structured = called["structuredContent"]
    assert isinstance(structured, dict)
    assert structured == {
        "status": "ready",
        "setup_url": "http://127.0.0.1:45123/setup/one-time",
        "expires_in_seconds": 300,
        "restart_recommended_after_save": True,
    }
    serialized = json.dumps(called)
    assert "webhooks/" not in serialized
    assert "control_capability" not in serialized


def test_notification_setup_tool_rejects_all_arguments() -> None:
    launcher = _SetupLauncher()
    server = McpServer(
        FakeDaemon(),
        _test_key(),
        notification_setup=launcher,
    )
    _ = server.handle_line(_request(1, "initialize", {"protocolVersion": "2025-11-25"}))
    assert server.handle_line(_notification("notifications/initialized")) is None

    response = server.handle_line(
        _request(
            4,
            "tools/call",
            {
                "name": "cmw.notifications.setup",
                "arguments": {"webhook_url": "must-not-enter-transcript"},
            },
        )
    )

    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32602
    assert launcher.calls == 0


def test_tools_call_parses_start_arguments_before_daemon() -> None:
    # Given
    daemon = FakeDaemon()
    server = _ready_server(daemon)
    params: dict[str, JsonValue] = {
        "name": "cmw.start",
        "arguments": {
            "session_id": "session-1",
            "control_capability": _capability("session-1"),
            "transcript_path": "C:/sessions/one.jsonl",
            "permission_mode": "bypassPermissions",
            "auto_restart": True,
        },
    }

    # When
    response = server.handle_line(_request(3, "tools/call", params))

    # Then
    assert daemon.started is not None
    assert daemon.started.session_id == "session-1"
    assert daemon.started.auto_restart is True
    structured = _success_result(response).get("structuredContent")
    assert isinstance(structured, dict)
    assert structured["status"] == "started"


def test_authenticated_goal_companion_precedes_bad_duration() -> None:
    # Given
    daemon = FakeDaemon()
    server = _ready_server(daemon)
    arguments: dict[str, JsonValue] = {
        "session_id": "goal-session",
        "control_capability": _capability("goal-session"),
        "goal_companion": True,
        "warning_after_ms": 0,
        "transcript_path": "C:/sessions/goal.jsonl",
    }

    # When
    response = server.handle_line(
        _request(13, "tools/call", {"name": "cmw.start", "arguments": arguments})
    )

    # Then
    assert response is not None
    assert "error" in response
    error = response["error"]
    assert error["code"] == -32602
    assert error.get("data") == {"code": "goal_companion_atomic_update_unavailable"}
    assert daemon.started is None


def test_tools_call_returns_exact_expected_daemon_failure() -> None:
    # Given
    daemon = FakeDaemon(failure=DaemonServiceError("session_not_found"))
    server = _ready_server(daemon)
    params: dict[str, JsonValue] = {
        "name": "cmw.status",
        "arguments": {
            "session_id": "missing",
            "control_capability": _capability("missing"),
        },
    }

    # When
    response = server.handle_line(_request(4, "tools/call", params))

    # Then
    result = _success_result(response)
    assert result["isError"] is True
    assert result["structuredContent"] == {"error": "session_not_found"}


def test_tools_call_rejects_missing_capability_without_daemon_mutation() -> None:
    # Given
    daemon = FakeDaemon()
    server = _ready_server(daemon)
    params: dict[str, JsonValue] = {
        "name": "cmw.stop",
        "arguments": {"session_id": "session-a"},
    }

    # When
    response = server.handle_line(_request("bad-args", "tools/call", params))

    # Then
    assert response is not None
    assert response["id"] == "bad-args"
    assert "error" in response
    assert response["error"]["code"] == -32001
    assert response["error"].get("data") == {"code": "cmw_unauthorized"}
    assert daemon.session_request is None


@pytest.mark.parametrize("name", ["cmw.start", "cmw.stop", "cmw.status", "cmw.complete"])
@pytest.mark.parametrize("capability_case", ["copied-wrong", "malformed", "non-string"])
def test_cross_session_control_is_unauthorized_before_daemon_mutation(
    name: str,
    capability_case: str,
) -> None:
    # Given
    daemon = FakeDaemon()
    server = _ready_server(daemon)
    capability: JsonValue
    if capability_case == "copied-wrong":
        capability = _capability("session-a")
    elif capability_case == "malformed":
        capability = "malformed"
    else:
        capability = 7
    arguments: dict[str, JsonValue] = {
        "session_id": "session-b",
        "control_capability": capability,
    }
    if name == "cmw.start":
        arguments["transcript_path"] = "C:/sessions/b.jsonl"

    # When
    response = server.handle_line(
        _request(11, "tools/call", {"name": name, "arguments": arguments})
    )

    # Then
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32001
    assert response["error"].get("data") == {"code": "cmw_unauthorized"}
    assert daemon.started is None
    assert daemon.session_request is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"session_id": 7, "control_capability": "malformed"},
        {"session_id": "x" * 65_537, "control_capability": "x" * 43},
        {"session_id": "session-a", "control_capability": "x" * 65_537},
    ],
    ids=("missing", "non-string-session", "overlong-session", "overlong-capability"),
)
def test_malformed_auth_fields_are_uniformly_unauthorized(
    arguments: dict[str, JsonValue],
) -> None:
    # Given
    daemon = FakeDaemon()
    server = _ready_server(daemon)

    # When
    response = server.handle_line(
        _request(
            12,
            "tools/call",
            {"name": "cmw.status", "arguments": arguments},
        )
    )

    # Then
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32001
    assert response["error"].get("data") == {"code": "cmw_unauthorized"}
    assert daemon.session_request is None


def test_ping_is_available_before_initialize() -> None:
    # Given
    server = McpServer(FakeDaemon(), _test_key())

    # When
    response = server.handle_line(_request(9, "ping", {}))

    # Then
    assert response == {"jsonrpc": "2.0", "id": 9, "result": {}}


def _ready_server(daemon: FakeDaemon) -> McpServer:
    server = McpServer(daemon, _test_key())
    _ = server.handle_line(_request(1, "initialize", {"protocolVersion": "2025-11-25"}))
    assert server.handle_line(_notification("notifications/initialized")) is None
    return server


def _test_key() -> bytes:
    return hashlib.sha256(b"cmw-public-test-key").digest()


def _capability(session_id: str) -> str:
    return derive_control_capability(_test_key(), session_id)


def _request(request_id: int | str, method: str, params: Mapping[str, JsonValue]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def _notification(method: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method})


def _success_result(response: JsonRpcResponse | None) -> JsonObject:
    assert response is not None
    assert "result" in response
    result = response["result"]
    assert isinstance(result, dict)
    return result
