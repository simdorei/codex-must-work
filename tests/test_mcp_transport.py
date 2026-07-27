import hashlib
import json
from io import StringIO
from typing import final

from scripts.daemon_models import SessionId, SessionRequest, StartRequest, ToolResult
from scripts.mcp_protocol import StdioStreams
from scripts.mcp_server import run_server


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


def test_run_server_keeps_notifications_off_stdout() -> None:
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
    run_server(_TransportDaemon(), StdioStreams(stdin, stdout, stderr), _test_key())

    # Then
    messages = stdout.getvalue().splitlines()
    assert len(messages) == 2
    assert '"id":1' in messages[0]
    assert '"id":2' in messages[1]
    assert stderr.getvalue() == ""


def test_run_server_discards_streaming_overlimit_and_continues() -> None:
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
    run_server(_TransportDaemon(), StdioStreams(stdin, stdout, StringIO()), _test_key())

    # Then
    messages = stdout.getvalue().splitlines()
    assert len(messages) == 3
    assert '"id":1' in messages[0]
    assert '"id":null' in messages[1]
    assert '"code":-32600' in messages[1]
    assert '"id":2' in messages[2]
