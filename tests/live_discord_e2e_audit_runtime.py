"""Bounded read-only adapters for the live Discord preflight."""
# ruff: noqa: EM101, TC001

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts import installed_generation
from scripts.app_server_client import AppServerError, ResidentAppServer
from scripts.install_errors import InstallPluginError
from scripts.state_io import JsonValue
from tests.live_discord_e2e_audit_preflight import (
    PreflightError,
    PreflightLocator,
    PreflightSnapshot,
)
from tests.live_discord_e2e_audit_records import decode_json


def collect_preflight(
    locator: PreflightLocator,
    discord_thread_id: str,
    codex_thread_id: str,
    expected_package_digest_sha256: str | None,
) -> PreflightSnapshot:
    """Collect only bounded read-only status, thread, and Goal observations."""
    installed_digest = validate_installed_generation(locator, expected_package_digest_sha256)
    authenticated, active = read_cmw_status(locator)
    app_thread_id, goal_status = read_app_server(codex_thread_id)
    return PreflightSnapshot(
        discord_thread_id=discord_thread_id,
        codex_thread_id=codex_thread_id,
        session_id=locator.session_id,
        locator_session_id=locator.session_id,
        locator_transcript_matches=True,
        actual_package_digest_sha256=installed_digest,
        locator_package_digest_sha256=locator.package_digest_sha256,
        expected_package_digest_sha256=expected_package_digest_sha256,
        permission_mode=locator.permission_mode,
        cmw_authenticated=authenticated,
        cmw_active=active,
        app_thread_id=app_thread_id,
        goal_status=goal_status,
    )


def validate_installed_generation(
    locator: PreflightLocator, expected_package_digest_sha256: str | None
) -> str:
    """Re-use the product gate before executing any installed package code."""
    root = locator.plugin_root
    parents = root.parents
    if (
        len(parents) < 5
        or parents[0].name != "codex-must-work"
        or parents[1].name != "codex-must-work-local"
        or parents[2].name != "cache"
        or parents[3].name != "plugins"
    ):
        raise PreflightError("installed_root_invalid")
    codex_home = parents[4]
    try:
        generation = installed_generation.require_session_generation(codex_home, root)
    except InstallPluginError as error:
        raise PreflightError("installed_generation_invalid") from error
    if (
        generation.root != root.resolve()
        or generation.digest != locator.package_digest_sha256
        or (
            expected_package_digest_sha256 is not None
            and generation.digest != expected_package_digest_sha256
        )
    ):
        raise PreflightError("installed_package_digest_mismatch")
    return generation.digest


def read_cmw_status(locator: PreflightLocator) -> tuple[bool, bool]:
    """Call the installed MCP status tool with its in-memory capability."""
    command, arguments, cwd, child_env = _mcp_command(locator.plugin_root)
    requests: tuple[dict[str, JsonValue], ...] = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "cmw-e2e-audit", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "cmw.status",
                "arguments": {
                    "session_id": locator.session_id,
                    "control_capability": locator.control_capability,
                },
            },
        },
    )
    stdin = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in requests)
    try:
        completed = subprocess.run(  # noqa: S603
            (command, *arguments),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=child_env,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreflightError("cmw_status_failed") from error
    if completed.returncode != 0:
        raise PreflightError("cmw_status_failed")
    response = _response(completed.stdout, 2)
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError") is not False:
        raise PreflightError("cmw_status_unauthenticated")
    content = result.get("structuredContent")
    if not isinstance(content, dict) or content.get("session_id") != locator.session_id:
        raise PreflightError("cmw_status_invalid")
    status = content.get("status")
    if status not in {"active", "inactive"}:
        raise PreflightError("cmw_status_invalid")
    return True, status == "active"


def read_app_server(codex_thread_id: str) -> tuple[str, str | None]:
    """Use only thread/read and thread/goal/get on a bounded app-server client."""
    try:
        with ResidentAppServer() as client:
            thread_result = client.request(
                "thread/read",
                {"threadId": codex_thread_id, "includeTurns": False},
                timeout_seconds=8,
            )
            goal_result = client.request(
                "thread/goal/get",
                {"threadId": codex_thread_id},
                timeout_seconds=8,
            )
    except AppServerError as error:
        raise PreflightError("app_server_read_failed") from error
    thread = thread_result.get("thread")
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str):
        raise PreflightError("app_server_thread_invalid")
    goal = goal_result.get("goal")
    if goal is None:
        return thread_id, None
    if not isinstance(goal, dict) or goal.get("threadId") != codex_thread_id:
        raise PreflightError("app_server_goal_invalid")
    goal_status = goal.get("status")
    if not isinstance(goal_status, str):
        raise PreflightError("app_server_goal_invalid")
    return thread_id, goal_status


def _mcp_command(plugin_root: Path) -> tuple[str, tuple[str, ...], Path, dict[str, str]]:
    try:
        decoded = decode_json((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PreflightError("mcp_manifest_invalid") from error
    if not isinstance(decoded, dict):
        raise PreflightError("mcp_manifest_invalid")
    servers = decoded.get("mcpServers")
    server = servers.get("codex-must-work") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        raise PreflightError("mcp_manifest_invalid")
    command = server.get("command")
    raw_args = server.get("args")
    raw_env = server.get("env", {})
    if not isinstance(command, str) or not isinstance(raw_args, list):
        raise PreflightError("mcp_manifest_invalid")
    if not all(isinstance(value, str) for value in raw_args):
        raise PreflightError("mcp_manifest_invalid")
    arguments = tuple(f"{value}" for value in raw_args)
    command_path = Path(command)
    resolved = (
        command_path if command_path.is_absolute() else (plugin_root / command_path).resolve()
    )
    child_env = os.environ.copy()
    if isinstance(raw_env, dict):
        child_env.update({key: value for key, value in raw_env.items() if isinstance(value, str)})
    return str(resolved), arguments, plugin_root, child_env


def _response(stdout: str, request_id: int) -> dict[str, JsonValue]:
    for line in stdout.splitlines():
        try:
            decoded = decode_json(line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and decoded.get("id") == request_id:
            return decoded
    raise PreflightError("mcp_response_missing")
