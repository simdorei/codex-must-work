"""Run an initialized MCP dispatcher over bounded line-delimited STDIO."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from scripts.mcp_limits import MAX_RAW_LINE_BYTES
from scripts.mcp_protocol import (
    JsonRpcError,
    JsonRpcResponse,
    StdioStreams,
    encode_message,
    error_response,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TextIO


class McpLineServer(Protocol):
    """Minimal dispatcher surface consumed by the STDIO loop."""

    def handle_line(self, raw_line: str) -> JsonRpcResponse | None:
        """Handle one raw JSON-RPC line."""
        ...


def serve_lines(server: McpLineServer, streams: StdioStreams) -> None:
    """Run until EOF while reserving stdout for MCP protocol messages."""
    for raw_line, framing_error in _bounded_lines(streams.stdin):
        response = (
            error_response(JsonRpcError(-32600, "Invalid Request"))
            if framing_error
            else server.handle_line(raw_line)
        )
        if response is None:
            continue
        try:
            _ = streams.stdout.write(f"{encode_message(response)}\n")
            streams.stdout.flush()
        except OSError as error:
            _ = streams.stderr.write(f"mcp_stdout_failed:{error}\n")
            raise


def _bounded_lines(stream: TextIO) -> Iterator[tuple[str, bool]]:  # noqa: C901
    current: list[str] = []
    current_bytes = 0
    over_limit = False
    while True:
        chunk = stream.read(8_192)
        if chunk == "":
            if current or over_limit:
                yield "".join(current), over_limit
            return
        pieces = chunk.split("\n")
        for piece in pieces[:-1]:
            if not over_limit:
                try:
                    piece_bytes = len(piece.encode("utf-8"))
                except UnicodeEncodeError:
                    piece_bytes = MAX_RAW_LINE_BYTES + 1
                if current_bytes + piece_bytes > MAX_RAW_LINE_BYTES:
                    over_limit = True
                    current.clear()
                else:
                    current.append(piece)
            yield "".join(current), over_limit
            current.clear()
            current_bytes = 0
            over_limit = False
        tail = pieces[-1]
        if tail and not over_limit:
            try:
                tail_bytes = len(tail.encode("utf-8"))
            except UnicodeEncodeError:
                tail_bytes = MAX_RAW_LINE_BYTES + 1
            if current_bytes + tail_bytes > MAX_RAW_LINE_BYTES:
                over_limit = True
                current.clear()
            else:
                current.append(tail)
                current_bytes += tail_bytes
