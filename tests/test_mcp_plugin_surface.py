from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Protocol

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


ROOT = Path(__file__).parents[1]
_LOAD_JSON: Final = _json_loader()


def _json(path: str) -> JsonObject:
    value = _LOAD_JSON((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_manifest_registers_existing_mcp_companion() -> None:
    # Given / When
    manifest = _json(".codex-plugin/plugin.json")

    # Then
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (ROOT / ".mcp.json").is_file()
    interface = manifest["interface"]
    assert isinstance(interface, dict)
    capabilities = interface["capabilities"]
    assert isinstance(capabilities, list)
    assert "MCP" in capabilities


def test_mcp_uses_direct_portable_python_without_resident_shell() -> None:
    # Given / When
    servers = _json(".mcp.json")["mcpServers"]
    assert isinstance(servers, dict)
    server = servers["codex-must-work"]

    # Then
    assert server == {
        "command": (
            "../../../../data/codex-must-work-codex-must-work-local/"
            "portable-python-3.12.13+20260510/python"
        ),
        "args": [
            "-B",
            "scripts/mcp_server.py",
            "--plugin-data",
            "../../../../data/codex-must-work-codex-must-work-local",
        ],
        "cwd": ".",
        "env": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        },
        "required": True,
        "startup_timeout_sec": 15,
        "tool_timeout_sec": 120,
        "supports_parallel_tool_calls": False,
    }
    serialized = json.dumps(server).lower()
    assert all(shell not in serialized for shell in ("powershell", "pwsh", "cmd.exe", "sh"))


def test_only_low_frequency_session_hook_remains() -> None:
    # Given / When
    hooks = _json("hooks/hooks.json")["hooks"]
    assert isinstance(hooks, dict)

    # Then
    assert list(hooks) == ["SessionStart"]
    session_start = hooks["SessionStart"]
    assert isinstance(session_start, list)
    group = session_start[0]
    assert isinstance(group, dict)
    handlers = group["hooks"]
    assert isinstance(handlers, list)
    handler = handlers[0]
    assert isinstance(handler, dict)
    assert handler["type"] == "command"
    command = handler["command"]
    windows_command = handler["commandWindows"]
    assert isinstance(command, str)
    assert isinstance(windows_command, str)
    assert "hook_event.py" in command
    assert "hook_event.py" in windows_command


def test_session_hook_launchers_reuse_the_installer_runtime() -> None:
    # Given
    expected = "portable-python-$Version"

    # When
    windows = (ROOT / "runtime/launch-python.ps1").read_text(encoding="utf-8")
    posix = (ROOT / "runtime/launch-python.sh").read_text(encoding="utf-8")

    # Then
    assert expected in windows
    assert "portable-python-$version" in posix
    assert "prepared_python" in posix


def test_package_contains_mcp_surface_and_all_listed_files_exist() -> None:
    # Given / When
    package = _LOAD_JSON((ROOT / "runtime/package-files.json").read_text(encoding="utf-8"))

    # Then
    assert isinstance(package, list)
    assert all(isinstance(path, str) for path in package)
    paths = [path for path in package if isinstance(path, str)]
    assert len(paths) == len(package)
    assert paths == sorted(paths, key=str.encode)
    assert ".mcp.json" in paths
    assert "scripts/app_server_activity.py" in paths
    assert "scripts/agent_identity.py" in paths
    assert "scripts/discord_notifications.py" in paths
    assert "scripts/discord_webhook.py" in paths
    assert "scripts/daemon_models.py" in paths
    assert "scripts/daemon_recovery.py" in paths
    assert "scripts/daemon_scheduler.py" in paths
    assert "scripts/daemon_service.py" in paths
    assert "scripts/daemon_task.py" in paths
    assert "scripts/mcp_protocol.py" in paths
    assert "scripts/mcp_server.py" in paths
    assert "scripts/notification_setup.py" in paths
    assert "scripts/notification_setup_page.py" in paths
    assert "scripts/watcher_notifications.py" in paths
    assert all((ROOT / path).is_file() for path in paths)
