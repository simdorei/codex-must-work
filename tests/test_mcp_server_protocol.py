from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from scripts.mcp_server import McpServer, configure_plugin_data
from tests.mcp_server_test_support import (
    FakeActivationTickets,
    FakeDaemon,
    control_key,
    ready_server,
    request,
    success_result,
)


def test_initialize_negotiates_supported_protocol() -> None:
    # Given
    server = McpServer(
        FakeDaemon(),
        control_key(),
        activation_tickets=FakeActivationTickets(),
    )
    initialize_request = request(
        1,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
    )

    # When
    response = server.handle_line(initialize_request)

    # Then
    assert success_result(response)["protocolVersion"] == "2025-06-18"


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
    server = ready_server(FakeDaemon())

    # When
    response = server.handle_line(request(2, "tools/list", {}))

    # Then
    tools = success_result(response).get("tools")
    assert isinstance(tools, list)
    names: set[str] = set()
    for tool in tools:
        assert isinstance(tool, dict)
        name = tool.get("name")
        assert isinstance(name, str)
        names.add(name)
    assert names == {
        "cmw.work_on",
        "cmw.stop",
        "cmw.status",
        "cmw.complete",
        "cmw.settings",
    }
    for tool in tools:
        assert isinstance(tool, dict)
        schema = tool.get("inputSchema")
        assert isinstance(schema, dict)
        required = schema.get("required")
        assert isinstance(required, list)
        assert "control_capability" not in required
        properties = schema.get("properties")
        assert isinstance(properties, dict)
        assert "control_capability" not in properties
        if tool["name"] == "cmw.work_on":
            assert "warning_after_ms" in properties
            assert "critical_after_ms" in properties
            assert "auto_restart" not in properties
            assert "restart_after_ms" not in properties


def test_legacy_cmw_start_is_not_a_public_tool() -> None:
    # Given
    server = ready_server(FakeDaemon())

    # When
    response = server.handle_line(
        request(
            29,
            "tools/call",
            {
                "name": "cmw.start",
                "arguments": {
                    "session_id": "session-a",
                },
            },
        )
    )

    # Then
    assert response is not None
    assert response.get("error") == {"code": -32600, "message": "Invalid Request"}


def test_ping_is_available_before_initialize() -> None:
    # Given
    server = McpServer(
        FakeDaemon(),
        control_key(),
        activation_tickets=FakeActivationTickets(),
    )

    # When
    response = server.handle_line(request(9, "ping", {}))

    # Then
    assert response == {"jsonrpc": "2.0", "id": 9, "result": {}}
