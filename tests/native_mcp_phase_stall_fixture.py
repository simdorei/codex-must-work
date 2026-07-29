"""Deterministic MCP phase stalls for bounded native-runtime tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import cast

STALL_SECONDS = 30
PHASE = os.environ["CMW_TEST_STALL_PHASE"]

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _stall(phase: str) -> None:
    if phase != PHASE:
        return
    _ = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", f"import time;time.sleep({STALL_SECONDS})"]
    )
    _ = sys.stderr.write(f"stall:{phase}")
    _ = sys.stderr.flush()
    time.sleep(STALL_SECONDS)


def _reply(response_id: int, result: JsonObject) -> None:
    _ = sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": response_id, "result": result},
            separators=(",", ":"),
        )
        + "\n"
    )
    _ = sys.stdout.flush()


for raw in sys.stdin:
    request = cast("JsonObject", json.loads(raw))
    method = request.get("method")
    if method == "initialize":
        _stall("initialize")
        _reply(1, {"protocolVersion": "2025-11-25", "capabilities": {}})
    elif method == "tools/list":
        _stall("tools_list")
        _reply(2, {"tools": []})

_stall("shutdown")
