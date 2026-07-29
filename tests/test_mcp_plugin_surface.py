from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final, Protocol

from scripts.mcp_tool_descriptors import control_tool_descriptors

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


def test_mcp_uses_bundled_native_windows_launcher() -> None:
    # Given / When
    servers = _json(".mcp.json")["mcpServers"]
    assert isinstance(servers, dict)
    server = servers["codex-must-work"]

    # Then
    assert server == {
        "command": "runtime/launch-python.exe",
        "args": [
            "scripts/mcp_bootstrap.py",
        ],
        "cwd": ".",
        "env": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        },
        "required": True,
        "startup_timeout_sec": 60,
        "tool_timeout_sec": 120,
        "supports_parallel_tool_calls": False,
    }
    serialized = json.dumps(server).lower()
    assert "codex-must-work-local" not in serialized
    assert "simdorei" not in serialized
    assert all(shell not in serialized for shell in ("powershell", "pwsh", "cmd.exe"))


def test_only_explicit_activation_prompt_hook_is_shipped() -> None:
    # Given / When
    hooks = _json("hooks/hooks.json")["hooks"]
    assert isinstance(hooks, dict)

    # Then
    assert list(hooks) == ["UserPromptSubmit"]
    user_prompt_submit = hooks["UserPromptSubmit"]
    assert isinstance(user_prompt_submit, list)
    assert len(user_prompt_submit) == 1
    activation_group = user_prompt_submit[0]
    assert isinstance(activation_group, dict)
    assert set(activation_group) == {"hooks"}
    activation_handlers = activation_group["hooks"]
    assert isinstance(activation_handlers, list)
    assert len(activation_handlers) == 1
    activation_handler = activation_handlers[0]
    assert isinstance(activation_handler, dict)
    assert activation_handler == {
        "type": "command",
        "command": (
            'sh "${PLUGIN_ROOT}/runtime/launch-python.sh" "${PLUGIN_ROOT}/scripts/work_on_hook.py"'
        ),
        "commandWindows": (
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            '-ExecutionPolicy Bypass -File "${PLUGIN_ROOT}/runtime/launch-python.ps1" '
            '-ForwardStdin "${PLUGIN_ROOT}/scripts/work_on_hook.py"'
        ),
        "timeout": 60,
    }
    serialized = json.dumps(hooks)
    assert "SessionStart" not in serialized
    assert "session_hook.py" not in serialized
    assert "codex_must_work_locator" not in serialized


def test_hook_launchers_reuse_the_installer_runtime() -> None:
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
    assert "scripts/app_server_activity.py" not in paths
    assert not any(path.startswith("scripts/manager") for path in paths)
    assert "scripts/agent_identity.py" in paths
    assert "scripts/discord_notifications.py" in paths
    assert "scripts/discord_webhook.py" in paths
    assert "scripts/daemon_models.py" not in paths
    assert "scripts/monitor_models.py" in paths
    assert "scripts/daemon_scheduler.py" in paths
    assert "scripts/notification_daemon.py" in paths
    assert "scripts/notification_session.py" in paths
    assert "scripts/session_hook.py" not in paths
    assert "scripts/hook_payload.py" not in paths
    assert "scripts/hook_event.py" not in paths
    assert "scripts/mcp_protocol.py" in paths
    assert "scripts/mcp_bootstrap.py" in paths
    assert "scripts/mcp_server.py" in paths
    assert "scripts/notification_setup.py" in paths
    assert "scripts/notification_setup_page.py" in paths
    assert "runtime/launch-python.exe" in paths
    assert "runtime/windows-launcher/Cargo.toml" in paths
    assert "runtime/windows-launcher/src/main.rs" in paths
    assert "scripts/watcher_notifications.py" in paths
    assert "scripts/threshold_settings.py" in paths
    assert "skills/work-settings/SKILL.md" in paths
    assert all((ROOT / path).is_file() for path in paths)


def test_activation_surface_exposes_only_explicit_work_on_control() -> None:
    # Given / When: the runtime builds the machine-consumed MCP descriptors.
    descriptors = control_tool_descriptors()
    names: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        name = descriptor.get("name")
        if isinstance(name, str):
            names.add(name)

    # Then: activation is routed only through the explicit work-on tool.
    assert names == {"cmw.work_on", "cmw.stop", "cmw.status", "cmw.complete", "cmw.settings"}


def test_exact_packaged_uninstaller_cli_is_read_only_for_clean_home(tmp_path: Path) -> None:
    package = _LOAD_JSON((ROOT / "runtime/package-files.json").read_text(encoding="utf-8"))
    assert isinstance(package, list)
    installed = tmp_path / "installed"
    for raw_path in package:
        if not isinstance(raw_path, str) or not raw_path.startswith("scripts/"):
            continue
        source = ROOT.joinpath(*raw_path.split("/"))
        destination = installed.joinpath(*raw_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(source, destination)
    environment = os.environ.copy()
    _ = environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    home = tmp_path / "clean-unowned-home"
    home.mkdir()
    sentinel = home / "preserve.bin"
    _ = sentinel.write_bytes(b"unowned")
    before = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/uninstall_plugin.py",
            str(home),
            str(installed),
        ],
        check=False,
        cwd=installed,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "config_changed": False,
        "preserved_data_roots": [],
        "purged_data_roots": 0,
        "removed_cache_generations": 0,
        "removed_runtime_roots": 0,
    }
    after = {
        path.relative_to(home).as_posix(): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == ["preserve.bin"]
