from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import pytest

from scripts.control_capability import provision_control_key
from scripts.work_on_activation import ActivationIdentity

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final = _json_loader()
_ROOT: Final = Path(__file__).parents[1]


@dataclass(frozen=True, slots=True)
class _McpCall:
    identity: ActivationIdentity
    environment: dict[str, str]


@pytest.mark.skipif(os.name != "nt", reason="native Windows activation E2E")
def test_explicit_hook_ticket_starts_once_and_replay_fails(tmp_path: Path) -> None:
    plugin_data, environment = _environment(tmp_path)
    transcript = tmp_path / "codex-home" / "rollout-a.jsonl"
    _ = transcript.write_text("", encoding="utf-8")
    payload = json.dumps(
        {
            "session_id": "session-a",
            "turn_id": "turn-a",
            "transcript_path": str(transcript),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "$work-on verify this task",
        }
    )

    hook = _run_hook(payload, environment)
    call = _McpCall(
        ActivationIdentity("session-a", "turn-a", str(transcript)),
        environment,
    )
    first = _run_work_on(call)
    replay = _run_work_on(call)

    assert hook.returncode == 0, hook.stderr
    hook_output = _json_object(_LOAD_JSON(hook.stdout))
    specific = _json_object(hook_output["hookSpecificOutput"])
    context = _json_object(_LOAD_JSON(str(specific["additionalContext"])))
    assert context == {
        "codex_must_work_activation": {
            "session_id": "session-a",
            "activation_turn_id": "turn-a",
            "transcript_path": str(transcript),
        }
    }
    assert "control_capability" not in hook.stdout
    assert _tool_payload(first, 2)["status"] == "active"
    assert _tool_payload(replay, 2) == {"error": "work_on_authorization_required"}
    assert plugin_data.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="native Windows activation E2E")
def test_direct_mcp_work_on_without_explicit_hook_fails_closed(tmp_path: Path) -> None:
    _, environment = _environment(tmp_path)
    transcript = tmp_path / "codex-home" / "rollout-direct.jsonl"
    _ = transcript.write_text("", encoding="utf-8")

    result = _run_work_on(
        _McpCall(
            ActivationIdentity("session-direct", "turn-direct", str(transcript)),
            environment,
        )
    )

    assert _tool_payload(result, 2) == {"error": "work_on_authorization_required"}
    assert "control_capability" not in result.stdout


def _environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    plugin_data = tmp_path / "plugin-data"
    _ = provision_control_key(plugin_data, codex_home / "codex-must-work")
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["PLUGIN_DATA"] = str(plugin_data)
    return plugin_data, environment


def _run_hook(payload: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    return subprocess.run(  # noqa: S603
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_ROOT / "runtime" / "launch-python.ps1"),
            "-ForwardStdin",
            str(_ROOT / "scripts" / "work_on_hook.py"),
        ],
        input=payload,
        check=False,
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_work_on(call: _McpCall) -> subprocess.CompletedProcess[str]:
    requests: tuple[JsonObject, ...] = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "cmw.work_on",
                "arguments": {
                    "session_id": call.identity.session_id,
                    "transcript_path": call.identity.transcript_path,
                    "activation_turn_id": call.identity.turn_id,
                },
            },
        },
    )
    stdin = "".join(json.dumps(request, separators=(",", ":")) + "\n" for request in requests)
    return subprocess.run(  # noqa: S603
        [str(_ROOT / "runtime" / "launch-python.exe"), "scripts/mcp_bootstrap.py"],
        input=stdin,
        check=False,
        cwd=_ROOT,
        env=call.environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _tool_payload(completed: subprocess.CompletedProcess[str], request_id: int) -> JsonObject:
    assert completed.returncode == 0, completed.stderr
    for line in completed.stdout.splitlines():
        response = _json_object(_LOAD_JSON(line))
        if response.get("id") == request_id:
            result = _json_object(response["result"])
            return _json_object(result["structuredContent"])
    message = f"missing MCP response: {request_id}"
    raise AssertionError(message)


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value
