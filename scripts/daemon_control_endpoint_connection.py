"""Authenticate and dispatch one bounded endpoint connection."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Final, final

from scripts.daemon_control_endpoint_auth import (
    encode_server_proof,
    parse_client_hello,
)
from scripts.mcp_limits import MAX_RAW_LINE_BYTES
from scripts.mcp_protocol import (
    JsonRpcError,
    decode_request,
    error_response,
)

if TYPE_CHECKING:
    import socket

    from scripts.daemon_control_endpoint_models import EndpointLocator, ServerFactory
    from scripts.mcp_protocol import DaemonBackend, JsonRpcResponse

_READ_CHUNK_BYTES: Final = 8_192
_PARSE_ERROR_CODE: Final = -32700
_CONTROL_TOOLS: Final = frozenset({"cmw.start", "cmw.stop", "cmw.status", "cmw.complete"})
_INITIALIZE: Final = (
    '{"jsonrpc":"2.0","id":0,"method":"initialize",'
    '"params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{}}}'
)
_INITIALIZED: Final = '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}'


@final
class EndpointConnectionHandler:
    """Serve the challenge and one capability-bearing MCP request."""

    def __init__(
        self,
        service: DaemonBackend,
        control_key: bytes,
        server_factory: ServerFactory,
    ) -> None:
        """Retain the daemon and MCP factory used after endpoint authentication."""
        self._service = service
        self._control_key = control_key
        self._server_factory = server_factory

    def handle(
        self,
        connection: socket.socket,
        locator: EndpointLocator,
    ) -> JsonRpcResponse | None:
        """Authenticate the generation before reading the MCP request."""
        hello = _read_line(connection)
        if hello is None:
            return None
        challenge = parse_client_hello(hello, locator)
        if challenge is None:
            return None
        try:
            connection.sendall(encode_server_proof(locator, challenge))
        except (OSError, TimeoutError):
            return None
        raw_request = _read_line(connection)
        if raw_request is None:
            return error_response(JsonRpcError(-32600, "Invalid Request"))
        return self._dispatch(raw_request, challenge)

    def _dispatch(self, raw_line: str, challenge: str) -> JsonRpcResponse:
        try:
            request = decode_request(raw_line)
        except JsonRpcError as error:
            if error.code == _PARSE_ERROR_CODE:
                return error_response(JsonRpcError(_PARSE_ERROR_CODE, "Parse error"))
            return error_response(error)
        request_challenge = request.params.get("endpoint_challenge")
        name = request.params.get("name")
        authorized = (
            request.method == "tools/call"
            and name in _CONTROL_TOOLS
            and type(request_challenge) is str
            and secrets.compare_digest(request_challenge, challenge)
        )
        if not authorized:
            return error_response(
                JsonRpcError(
                    -32001,
                    "Unauthorized",
                    request_id=request.request_id,
                    data={"code": "cmw_unauthorized"},
                )
            )
        server = self._server_factory(self._service, self._control_key)
        _ = server.handle_line(_INITIALIZE)
        _ = server.handle_line(_INITIALIZED)
        response = server.handle_line(raw_line)
        return (
            response
            if response is not None
            else error_response(JsonRpcError(-32600, "Invalid Request"))
        )


def _read_line(connection: socket.socket) -> str | None:
    raw = bytearray()
    try:
        while b"\n" not in raw and len(raw) <= MAX_RAW_LINE_BYTES:
            chunk = connection.recv(_READ_CHUNK_BYTES)
            if not chunk:
                break
            raw.extend(chunk)
    except (OSError, TimeoutError):
        return None
    if len(raw) > MAX_RAW_LINE_BYTES:
        return None
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return None
