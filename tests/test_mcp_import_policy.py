from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final, Protocol

import pytest

from scripts.control_capability import provision_control_key
from scripts.mcp_bootstrap import ALLOWED_SCRIPTS_MODULES
from scripts.private_root import ensure_private_root
from scripts.work_on_activation import ActivationIdentity, ActivationTicketStore

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _json_loader() -> _JsonLoader:
    return json.loads


_LOAD_JSON: Final[_JsonLoader] = _json_loader()
_ROOT: Final = Path(__file__).parents[1]
_DENIED_MODULE: Final = "scripts.session_hook"


def test_bootstrap_import_is_inert_and_policy_passes_non_scripts() -> None:
    """Given a normal import, policy installation remains explicit and stdlib still resolves."""
    code = (
        "import sys\n"
        "before = tuple(sys.meta_path)\n"
        "from scripts.mcp_bootstrap import install_import_policy\n"
        "assert tuple(sys.meta_path) == before\n"
        "install_import_policy()\n"
        "import email.mime.text\n"
        "print('ok')\n"
    )

    completed = _run_python(code)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ok\n"


@pytest.mark.parametrize(
    "statement",
    [
        "import scripts.session_hook as forbidden_alias",
        "from scripts import session_hook",
        "import importlib; importlib.import_module('.session_hook', 'scripts')",
        "import importlib; assigned = importlib.import_module; assigned('scripts.session_hook')",
        "__import__('scripts', fromlist=['session_hook'])",
    ],
)
def test_runtime_policy_rejects_every_cpython_import_spelling(statement: str) -> None:
    """Given a forbidden identity, CPython routes every spelling through the policy."""
    code = (
        "import json\n"
        "from scripts.mcp_bootstrap import install_import_policy\n"
        "install_import_policy()\n"
        "try:\n"
        f"    exec({statement!r})\n"
        "except ImportError as error:\n"
        "    print(json.dumps({'type': type(error).__name__, "
        "'reason': getattr(error, 'reason_code', None), "
        "'module': getattr(error, 'module_name', None)}))\n"
        "else:\n"
        "    raise SystemExit(7)\n"
    )

    completed = _run_python(code)

    assert completed.returncode == 0, completed.stderr
    assert _LOAD_JSON(completed.stdout) == {
        "type": "ScriptsImportDeniedError",
        "reason": "scripts_import_denied",
        "module": _DENIED_MODULE,
    }


def test_runtime_policy_rejects_package_before_its_initializer_runs(tmp_path: Path) -> None:
    """Given a forbidden package, its initializer cannot execute."""
    package_root = tmp_path / "scripts" / "forbidden_package"
    package_root.mkdir(parents=True)
    marker = tmp_path / "initializer-ran"
    _ = (package_root / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    code = (
        "import json, scripts\n"
        f"scripts.__path__.insert(0, {str(tmp_path / 'scripts')!r})\n"
        "from scripts.mcp_bootstrap import install_import_policy\n"
        "install_import_policy()\n"
        "try:\n"
        "    import scripts.forbidden_package\n"
        "except ImportError as error:\n"
        "    print(json.dumps({'reason': getattr(error, 'reason_code', None), "
        "'module': getattr(error, 'module_name', None)}))\n"
        "else:\n"
        "    raise SystemExit(7)\n"
    )

    completed = _run_python(code)

    assert completed.returncode == 0, completed.stderr
    assert _LOAD_JSON(completed.stdout) == {
        "reason": "scripts_import_denied",
        "module": "scripts.forbidden_package",
    }
    assert not marker.exists()


def test_policy_excludes_control_and_continuation_identities() -> None:
    """Given the audited list, no control-plane identity is permitted."""
    forbidden = {
        "scripts.app_server",
        "scripts.continuation",
        "scripts.goal_control",
        "scripts.manager_restart_guard",
        "scripts.session_hook",
    }

    assert ALLOWED_SCRIPTS_MODULES.isdisjoint(forbidden)
    assert not any(
        module.startswith(
            (
                "scripts.app_server",
                "scripts.continuation",
                "scripts.goal_",
                "scripts.manager",
            )
        )
        for module in ALLOWED_SCRIPTS_MODULES
    )


def test_real_bootstrap_initializes_lists_and_starts_observation(
    tmp_path: Path,
) -> None:
    """Given the shipped bootstrap, real MCP requests load only audited shipped modules."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    plugin_data = tmp_path / "plugin-data"
    ensure_private_root(plugin_data)
    key = provision_control_key(plugin_data, codex_home / "codex-must-work")
    session_id = "runtime-policy-smoke"
    transcript = codex_home / "sessions" / "rollout.jsonl"
    transcript.parent.mkdir()
    _ = transcript.write_text("", encoding="utf-8")
    activation_turn_id = "turn-runtime-policy"
    _ = ActivationTicketStore(plugin_data, key).issue(
        ActivationIdentity(session_id, activation_turn_id, str(transcript))
    )
    requests: tuple[dict[str, JsonValue], ...] = (
        _request(1, "initialize", _initialize_params()),
        _initialized_notification(),
        _request(2, "tools/list", {}),
        _request(
            3,
            "tools/call",
            {
                "name": "cmw.work_on",
                "arguments": {
                    "session_id": session_id,
                    "transcript_path": str(transcript),
                    "activation_turn_id": activation_turn_id,
                    "warning_after_ms": 90_000,
                    "critical_after_ms": 300_000,
                },
            },
        ),
    )
    stdin = "".join(json.dumps(request, separators=(",", ":")) + "\n" for request in requests)
    code = (
        "import json, sys\n"
        "from scripts.mcp_bootstrap import main\n"
        "result = main([])\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('scripts.'))\n"
        "print('CMW_IMPORT_AUDIT=' + json.dumps(loaded), file=sys.stderr)\n"
        "raise SystemExit(result)\n"
    )
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["PLUGIN_DATA"] = str(plugin_data)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = _run_python(code, stdin=stdin, environment=environment)

    responses = [_LOAD_JSON(line) for line in completed.stdout.splitlines()]
    assert completed.returncode == 0, completed.stderr
    assert _response(responses, 1).get("result") is not None
    listed = _response(responses, 2)["result"]
    assert isinstance(listed, dict)
    tools = listed["tools"]
    assert isinstance(tools, list)
    assert any(isinstance(tool, dict) and tool.get("name") == "cmw.work_on" for tool in tools)
    started = _response(responses, 3)["result"]
    assert isinstance(started, dict)
    structured = started["structuredContent"]
    assert isinstance(structured, dict)
    assert structured["status"] == "active"
    audit_prefix = "CMW_IMPORT_AUDIT="
    assert completed.stderr.startswith(audit_prefix)
    loaded_value = _LOAD_JSON(completed.stderr.removeprefix(audit_prefix))
    assert isinstance(loaded_value, list)
    loaded = {name for name in loaded_value if isinstance(name, str)}
    shipped = _shipped_modules()
    assert loaded == ALLOWED_SCRIPTS_MODULES
    assert loaded <= shipped


def _run_python(
    code: str,
    *,
    stdin: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", code],
        cwd=_ROOT,
        env=environment,
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _request(request_id: int, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _initialize_params() -> dict[str, JsonValue]:
    return {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "runtime-policy-smoke", "version": "1"},
    }


def _initialized_notification() -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}


def _response(responses: list[JsonValue], request_id: int) -> dict[str, JsonValue]:
    for response in responses:
        if isinstance(response, dict) and response.get("id") == request_id:
            return response
    raise AssertionError(request_id)


def _shipped_modules() -> set[str]:
    value = _LOAD_JSON((_ROOT / "runtime" / "package-files.json").read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return {
        path.removesuffix(".py").replace("/", ".")
        for path in value
        if isinstance(path, str) and path.startswith("scripts/") and path.endswith(".py")
    }
