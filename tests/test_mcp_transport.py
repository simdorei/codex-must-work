import hashlib
import json
import os
import queue
import threading
from io import StringIO
from pathlib import Path
from typing import final

import pytest

from scripts.mcp_protocol import StdioStreams
from scripts.mcp_server import run_server
from scripts.monitor_models import SessionId, SessionRequest, StartRequest, ToolResult
from scripts.private_root import ensure_private_root
from scripts.work_on_activation import ActivationTicketStore


@final
class _TransportDaemon:
    def start(self, request: StartRequest) -> ToolResult:
        _ = request
        return ToolResult(SessionId("transport"), "started")

    def stop(self, request: SessionRequest) -> ToolResult:
        _ = request
        return ToolResult(SessionId("transport"), "stopped")

    def status(self, request: SessionRequest) -> ToolResult:
        _ = request
        return ToolResult(SessionId("transport"), "active")

    def complete(self, request: SessionRequest) -> ToolResult:
        _ = request
        return ToolResult(SessionId("transport"), "completed")

    def close(self) -> None:
        return


def _request(request_id: int, method: str, params: dict[str, str]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def _notification(method: str) -> str:
    return '{"jsonrpc":"2.0","method":"' + method + '"}'


def _test_key() -> bytes:
    return hashlib.sha256(b"cmw-public-test-key").digest()


@pytest.fixture
def activation_tickets(tmp_path: Path) -> ActivationTicketStore:
    plugin_data = tmp_path / "plugin-data"
    ensure_private_root(plugin_data)
    return ActivationTicketStore(plugin_data, _test_key())


def test_run_server_keeps_notifications_off_stdout(
    activation_tickets: ActivationTicketStore,
) -> None:
    # Given
    stdin = StringIO(
        "\n".join(
            (
                _request(1, "initialize", {"protocolVersion": "2025-11-25"}),
                _notification("notifications/initialized"),
                _request(2, "ping", {}),
            )
        )
        + "\n"
    )
    stdout = StringIO()
    stderr = StringIO()

    # When
    run_server(
        _TransportDaemon(),
        StdioStreams(stdin, stdout, stderr),
        _test_key(),
        activation_tickets=activation_tickets,
    )

    # Then
    messages = stdout.getvalue().splitlines()
    assert len(messages) == 2
    assert '"id":1' in messages[0]
    assert '"id":2' in messages[1]
    assert stderr.getvalue() == ""


def test_run_server_discards_streaming_overlimit_and_continues(
    activation_tickets: ActivationTicketStore,
) -> None:
    # Given
    oversized = "{" + "x" * 1_048_576 + "}"
    stdin = StringIO(
        "\n".join(
            (
                _request(1, "initialize", {"protocolVersion": "2025-11-25"}),
                oversized,
                _request(2, "ping", {}),
            )
        )
        + "\n"
    )
    stdout = StringIO()

    # When
    run_server(
        _TransportDaemon(),
        StdioStreams(stdin, stdout, StringIO()),
        _test_key(),
        activation_tickets=activation_tickets,
    )

    # Then
    messages = stdout.getvalue().splitlines()
    assert len(messages) == 3
    assert '"id":1' in messages[0]
    assert '"id":null' in messages[1]
    assert '"code":-32600' in messages[1]
    assert '"id":2' in messages[2]


def test_run_server_replies_to_pipe_initialize_before_eof(
    activation_tickets: ActivationTicketStore,
) -> None:
    # Given
    stdin_read_descriptor, stdin_write_descriptor = os.pipe()
    stdout_read_descriptor, stdout_write_descriptor = os.pipe()
    stdin = os.fdopen(stdin_read_descriptor, encoding="utf-8", newline="")
    client = os.fdopen(stdin_write_descriptor, "w", encoding="utf-8", newline="")
    server = os.fdopen(stdout_write_descriptor, "w", encoding="utf-8", newline="")
    responses = os.fdopen(stdout_read_descriptor, encoding="utf-8", newline="")
    received: queue.Queue[str] = queue.Queue()

    server_worker = threading.Thread(
        target=run_server,
        args=(_TransportDaemon(), StdioStreams(stdin, server, StringIO()), _test_key()),
        kwargs={"activation_tickets": activation_tickets},
        daemon=True,
    )
    response_worker = threading.Thread(
        target=lambda: received.put(responses.readline()),
        daemon=True,
    )
    server_worker.start()
    response_worker.start()

    try:
        # When
        _ = client.write(_request(1, "initialize", {"protocolVersion": "2025-11-25"}) + "\n")
        client.flush()

        # Then
        try:
            response = received.get(timeout=1.0)
        except queue.Empty:
            pytest.fail("MCP server waited for pipe EOF instead of replying to initialize")
        assert '"id":1' in response
        assert '"protocolVersion":"2025-11-25"' in response
    finally:
        client.close()
        server_worker.join(timeout=1.0)
        server.close()
        response_worker.join(timeout=1.0)
        stdin.close()
        responses.close()
