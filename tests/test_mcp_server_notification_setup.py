from __future__ import annotations

import json
from typing import final

from scripts.mcp_server import McpServer
from scripts.notification_setup import NotificationSetupLaunch
from tests.mcp_server_test_support import (
    FakeActivationTickets,
    FakeDaemon,
    control_key,
    notification,
    request,
    success_result,
)


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
    assert schema["required"] == ["session_id"]

    called = success_result(
        server.handle_line(
            request(
                3,
                "tools/call",
                {
                    "name": "cmw.notifications.setup",
                    "arguments": {
                        "session_id": "setup-session",
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
                    "webhook_url": "must-not-enter-transcript",
                },
            },
        )
    )

    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32602
    assert launcher.calls == 0


def test_notification_setup_rejects_missing_session_before_launcher_mutation() -> None:
    launcher = _SetupLauncher()
    server = _setup_server(launcher)
    response = server.handle_line(
        request(
            5,
            "tools/call",
            {"name": "cmw.notifications.setup", "arguments": {}},
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
