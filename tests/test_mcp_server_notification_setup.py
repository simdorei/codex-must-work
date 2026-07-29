from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, assert_never, final

import pytest

if TYPE_CHECKING:
    from scripts.state_io import JsonValue

from scripts.mcp_server import McpServer
from scripts.notification_setup import NotificationSetupLaunch
from tests.mcp_server_test_support import (
    FakeActivationTickets,
    FakeDaemon,
    capability,
    control_key,
    notification,
    request,
    success_result,
)

type _SetupCapabilityCase = Literal["missing", "cross-session", "malformed", "non-string"]


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
    server = _setup_server(launcher)

    listed = success_result(server.handle_line(request(2, "tools/list", {})))
    tools = listed["tools"]
    assert isinstance(tools, list)
    names: set[str] = set()
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                names.add(name)
    assert "cmw.notifications.setup" in names
    setup_tool = next(
        tool
        for tool in tools
        if isinstance(tool, dict) and tool.get("name") == "cmw.notifications.setup"
    )
    schema = setup_tool["inputSchema"]
    assert isinstance(schema, dict)
    assert schema["required"] == ["session_id", "control_capability"]

    called = success_result(
        server.handle_line(
            request(
                3,
                "tools/call",
                {
                    "name": "cmw.notifications.setup",
                    "arguments": {
                        "session_id": "setup-session",
                        "control_capability": capability("setup-session"),
                    },
                },
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


def test_notification_setup_tool_rejects_unknown_arguments_after_authorization() -> None:
    launcher = _SetupLauncher()
    server = _setup_server(launcher)

    response = server.handle_line(
        request(
            4,
            "tools/call",
            {
                "name": "cmw.notifications.setup",
                "arguments": {
                    "session_id": "setup-session",
                    "control_capability": capability("setup-session"),
                    "webhook_url": "must-not-enter-transcript",
                },
            },
        )
    )

    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32602
    assert launcher.calls == 0


@pytest.mark.parametrize(
    "capability_case",
    ["missing", "cross-session", "malformed", "non-string"],
)
def test_notification_setup_rejects_unauthorized_calls_before_launcher_mutation(
    capability_case: _SetupCapabilityCase,
) -> None:
    launcher = _SetupLauncher()
    server = _setup_server(launcher)
    arguments = _unauthorized_setup_arguments(capability_case)

    response = server.handle_line(
        request(
            5,
            "tools/call",
            {"name": "cmw.notifications.setup", "arguments": arguments},
        )
    )

    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32001
    assert response["error"].get("data") == {"code": "cmw_unauthorized"}
    assert launcher.calls == 0


def _setup_server(launcher: _SetupLauncher) -> McpServer:
    server = McpServer(
        FakeDaemon(),
        control_key(),
        activation_tickets=FakeActivationTickets(),
        notification_setup=launcher,
    )
    _ = server.handle_line(request(1, "initialize", {"protocolVersion": "2025-11-25"}))
    assert server.handle_line(notification("notifications/initialized")) is None
    return server


def _unauthorized_setup_arguments(
    capability_case: _SetupCapabilityCase,
) -> dict[str, JsonValue]:
    match capability_case:
        case "missing":
            return {}
        case "cross-session":
            return {
                "session_id": "setup-b",
                "control_capability": capability("setup-a"),
            }
        case "malformed":
            return {"session_id": "setup-b", "control_capability": "malformed"}
        case "non-string":
            return {"session_id": "setup-b", "control_capability": 7}
        case _:
            assert_never(capability_case)
