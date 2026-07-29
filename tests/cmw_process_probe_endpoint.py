"""Strict client for the daemon's private one-request control endpoint."""

from __future__ import annotations

import json
import secrets
import socket
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast, final, override

from scripts.daemon_control_endpoint import EndpointLocator, control_endpoint_path
from scripts.daemon_control_endpoint_auth import (
    encode_client_hello,
    verify_server_proof,
)
from scripts.daemon_control_endpoint_identity import (
    ProcessIdentityError,
    process_created_ns,
)
from scripts.mcp_limits import MAX_RAW_LINE_BYTES
from scripts.state_io import JsonValue, ensure_direct_regular_file

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.mcp_protocol import JsonObject

type ControlTool = Literal["cmw.work_on", "cmw.stop", "cmw.status", "cmw.complete"]
_SCHEMA_VERSION: Final = 1
_TIMEOUT_SECONDS: Final = 3.0
_LOCATOR_UNAVAILABLE: Final = "control_endpoint_locator_unavailable"
_LOCATOR_INVALID: Final = "control_endpoint_locator_invalid"
_PROCESS_UNAVAILABLE: Final = "control_endpoint_process_unavailable"
_PROCESS_REUSED: Final = "control_endpoint_process_reused"
_ENDPOINT_UNAVAILABLE: Final = "control_endpoint_unavailable"
_SERVER_AUTH_FAILED: Final = "control_endpoint_server_auth_failed"
_REQUEST_FAILED: Final = "control_endpoint_request_failed"
_RESPONSE_INVALID: Final = "control_endpoint_response_invalid"
_RESPONSE_TOO_LARGE: Final = "control_endpoint_response_too_large"


class _JsonLoader(Protocol):
    def __call__(self, value: str) -> JsonValue: ...


def _json_loader(value: str) -> JsonValue:
    return cast("JsonValue", json.loads(value))


_LOAD_JSON: Final[_JsonLoader] = _json_loader


class _ChallengeFactory(Protocol):
    def __call__(self, byte_count: int) -> str: ...


def _challenge_factory(byte_count: int) -> str:
    return secrets.token_urlsafe(byte_count)


@final
class EndpointAttachError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code

    @override
    def __str__(self) -> str:
        return self.reason_code


def load_control_endpoint(plugin_data: Path, expected_pid: int) -> EndpointLocator:
    """Load and validate one exact current endpoint generation."""
    path = control_endpoint_path(plugin_data)
    try:
        ensure_direct_regular_file(plugin_data, path)
        decoded = _LOAD_JSON(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EndpointAttachError(_LOCATOR_UNAVAILABLE) from error
    if type(decoded) is not dict or decoded.get("schema_version") != _SCHEMA_VERSION:
        raise EndpointAttachError(_LOCATOR_INVALID)
    pid = decoded.get("pid")
    created = decoded.get("process_created_ns")
    port = decoded.get("port")
    nonce = decoded.get("endpoint_nonce")
    if not (
        type(pid) is int
        and pid == expected_pid
        and type(created) is int
        and created > 0
        and type(port) is int
        and 0 < port <= 65_535
        and type(nonce) is str
        and len(nonce) >= 32
    ):
        raise EndpointAttachError(_LOCATOR_INVALID)
    try:
        actual_created = process_created_ns(pid)
    except (OSError, ValueError, ProcessIdentityError) as error:
        raise EndpointAttachError(_PROCESS_UNAVAILABLE) from error
    if actual_created != created:
        raise EndpointAttachError(_PROCESS_REUSED)
    return EndpointLocator(pid, created, port, nonce)


@final
class EndpointClient:
    """Send one authenticated control request per bounded connection."""

    def __init__(
        self,
        locator: EndpointLocator,
        challenge_factory: _ChallengeFactory = _challenge_factory,
    ) -> None:
        self._locator: EndpointLocator = locator
        self._challenge_factory = challenge_factory

    def call(self, tool: ControlTool, arguments: JsonObject) -> JsonObject:
        challenge = self._challenge_factory(32)
        payload: JsonObject = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": arguments,
                "endpoint_challenge": challenge,
            },
        }
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            with socket.create_connection(
                ("127.0.0.1", self._locator.port),
                timeout=_TIMEOUT_SECONDS,
            ) as connection:
                connection.settimeout(_TIMEOUT_SECONDS)
                connection.sendall(encode_client_hello(self._locator, challenge))
                proof = _read_line(connection)
                if not verify_server_proof(proof, self._locator, challenge):
                    raise EndpointAttachError(_SERVER_AUTH_FAILED)
                try:
                    current_created = process_created_ns(self._locator.pid)
                except (OSError, ValueError, ProcessIdentityError) as error:
                    raise EndpointAttachError(_PROCESS_UNAVAILABLE) from error
                if current_created != self._locator.process_created_ns:
                    raise EndpointAttachError(_PROCESS_REUSED)
                connection.sendall(encoded)
                connection.shutdown(socket.SHUT_WR)
                response = _decode_response(_read_line(connection))
        except (OSError, TimeoutError) as error:
            raise EndpointAttachError(_ENDPOINT_UNAVAILABLE) from error
        result = response.get("result")
        if type(result) is not dict:
            raise EndpointAttachError(_REQUEST_FAILED)
        structured = result.get("structuredContent")
        if type(structured) is not dict:
            raise EndpointAttachError(_RESPONSE_INVALID)
        return structured


def _read_line(connection: socket.socket) -> str:
    raw = bytearray()
    while b"\n" not in raw and len(raw) <= MAX_RAW_LINE_BYTES:
        chunk = connection.recv(8_192)
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > MAX_RAW_LINE_BYTES:
        raise EndpointAttachError(_RESPONSE_TOO_LARGE)
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        raise EndpointAttachError(_RESPONSE_INVALID) from error


def _decode_response(raw_line: str) -> JsonObject:
    try:
        decoded = _LOAD_JSON(raw_line)
    except json.JSONDecodeError as error:
        raise EndpointAttachError(_RESPONSE_INVALID) from error
    if type(decoded) is not dict:
        raise EndpointAttachError(_RESPONSE_INVALID)
    return decoded
