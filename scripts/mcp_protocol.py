"""Small dependency-free JSON-RPC boundary for the CMW MCP server."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Final,
    Literal,
    NotRequired,
    Protocol,
    TextIO,
    TypedDict,
    override,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.daemon_models import SessionRequest, StartRequest, ToolResult
    from scripts.state_io import JsonValue

from scripts.mcp_limits import (
    MAX_RAW_LINE_BYTES,
    DuplicateMemberError,
    IntegerLimitError,
    MemberLimitError,
    MemberNameLimitError,
    NonFiniteNumberError,
    object_pairs,
    parse_int,
    reject_non_finite,
    validate_structure,
)

type JsonRpcId = int | str
type JsonObject = dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(
        self,
        s: str,
        *,
        object_pairs_hook: Callable[[list[tuple[str, JsonValue]]], JsonObject],
        parse_constant: Callable[[str], JsonValue],
        parse_int: Callable[[str], int],
    ) -> JsonValue: ...


def _stdlib_json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _stdlib_json_loader()


_LATEST_PROTOCOL: Final = "2025-11-25"
_SUPPORTED_PROTOCOLS: Final = frozenset(
    {"2024-11-05", "2025-03-26", "2025-06-18", _LATEST_PROTOCOL}
)


class JsonRpcErrorBody(TypedDict):
    """JSON-RPC error fields serialized on the wire."""

    code: int
    message: str
    data: NotRequired[JsonValue]


class JsonRpcSuccessResponse(TypedDict):
    """Successful JSON-RPC response wire shape."""

    jsonrpc: Literal["2.0"]
    id: JsonRpcId
    result: JsonValue


class JsonRpcFailureResponse(TypedDict):
    """Failed JSON-RPC response wire shape."""

    jsonrpc: Literal["2.0"]
    id: JsonRpcId | None
    error: JsonRpcErrorBody


type JsonRpcResponse = JsonRpcSuccessResponse | JsonRpcFailureResponse


class DaemonBackend(Protocol):
    """Typed lifecycle/control surface implemented by ``DaemonService``."""

    def start(self, request: StartRequest) -> ToolResult:
        """Enable or reuse one explicit CMW task."""
        ...

    def stop(self, request: SessionRequest) -> ToolResult:
        """Stop one explicit CMW task."""
        ...

    def status(self, request: SessionRequest) -> ToolResult:
        """Return one explicit CMW task status."""
        ...

    def complete(self, request: SessionRequest) -> ToolResult:
        """Request verified completion for one CMW task."""
        ...

    def close(self) -> None:
        """Release daemon-owned runtime resources."""
        ...


@dataclass(frozen=True, slots=True)
class StdioStreams:
    """Streams dedicated to one STDIO MCP connection."""

    stdin: TextIO
    stdout: TextIO
    stderr: TextIO


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    """One validated MCP request or notification."""

    method: str
    params: dict[str, JsonValue]
    request_id: JsonRpcId | None
    is_notification: bool


@dataclass(frozen=True, slots=True)
class JsonRpcError(ValueError):
    """A JSON-RPC protocol failure suitable for a response error object."""

    code: int
    message: str
    request_id: JsonRpcId | None = None
    data: JsonValue = None

    @override
    def __str__(self) -> str:
        return self.message


def decode_request(raw_line: str) -> JsonRpcRequest:  # noqa: C901, PLR0912
    """Decode and validate one newline-delimited JSON-RPC message."""
    line = raw_line.removesuffix("\n").removesuffix("\r")
    try:
        if len(line.encode("utf-8")) > MAX_RAW_LINE_BYTES:
            raise JsonRpcError(-32600, "Invalid Request")
    except UnicodeEncodeError as error:
        raise JsonRpcError(-32700, "Parse error") from error
    try:
        decoded: JsonValue = _LOAD_JSON(
            line,
            object_pairs_hook=object_pairs,
            parse_constant=reject_non_finite,
            parse_int=parse_int,
        )
    except DuplicateMemberError as error:
        raise JsonRpcError(-32600, "Invalid Request") from error
    except MemberLimitError as error:
        raise JsonRpcError(-32600, "Invalid Request") from error
    except MemberNameLimitError as error:
        raise JsonRpcError(-32600, "Invalid Request") from error
    except NonFiniteNumberError as error:
        raise JsonRpcError(-32700, "Parse error") from error
    except IntegerLimitError as error:
        raise JsonRpcError(-32700, "Parse error") from error
    except json.JSONDecodeError as error:
        raise JsonRpcError(code=-32700, message="Parse error", data=str(error)) from error
    if type(decoded) is not dict:
        raise JsonRpcError(code=-32600, message="Invalid Request") from None
    values: JsonObject = decoded
    try:
        validate_structure(values)
    except ValueError as error:
        raise JsonRpcError(-32600, "Invalid Request") from error
    request_id, notification = _request_id(values)
    if values.get("jsonrpc") != "2.0":
        raise JsonRpcError(-32600, "Invalid Request", request_id=request_id)
    method = values.get("method")
    if type(method) is not str or not method:
        raise JsonRpcError(-32600, "Invalid Request", request_id=request_id) from None
    params = values.get("params", {})
    if type(params) is not dict:
        raise JsonRpcError(-32600, "Invalid Request", request_id=request_id) from None
    return JsonRpcRequest(
        method=method,
        params=params,
        request_id=request_id,
        is_notification=notification,
    )


def result_response(request_id: JsonRpcId, result: JsonValue) -> JsonRpcSuccessResponse:
    """Build a successful JSON-RPC response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(fault: JsonRpcError) -> JsonRpcFailureResponse:
    """Build a JSON-RPC error response without hiding its diagnostic data."""
    error: JsonRpcErrorBody = {"code": fault.code, "message": fault.message}
    if fault.data is not None:
        error["data"] = fault.data
    return {"jsonrpc": "2.0", "id": fault.request_id, "error": error}


def encode_message(message: JsonRpcResponse) -> str:
    """Encode one UTF-8-safe compact JSON-RPC output line."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def negotiate_protocol(requested: str) -> str:
    """Select the requested supported MCP revision or the latest revision."""
    return requested if requested in _SUPPORTED_PROTOCOLS else _LATEST_PROTOCOL


def _request_id(values: dict[str, JsonValue]) -> tuple[JsonRpcId | None, bool]:
    if "id" not in values:
        return None, True
    request_id = values["id"]
    if request_id is None:
        return None, False
    if type(request_id) is int:
        return request_id, False
    if type(request_id) is str and request_id:
        return request_id, False
    raise JsonRpcError(-32600, "Invalid Request")
