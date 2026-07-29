from __future__ import annotations

import hashlib
import json
import re
import tarfile
from typing import cast

from scripts.mcp_bootstrap import ALLOWED_SCRIPTS_MODULES
from tests.test_portable_runtime import ARCHIVES, ROOT


def test_runtime_bundle_contains_all_pinned_archives() -> None:
    # Given / When: the packaged runtime archives are hashed.
    actual = {
        name: hashlib.sha256((ROOT / "runtime" / "archives" / name).read_bytes()).hexdigest()
        for name in ARCHIVES
    }

    # Then: every approved target is present with its release digest.
    assert actual == ARCHIVES


def test_runtime_archives_contain_executables_and_licenses() -> None:
    expected = {
        "cpython-3.12.13+20260510-windows-x64.tar.gz": (
            "python/python.exe",
            "python/LICENSE.txt",
        ),
        "cpython-3.12.13+20260510-linux-x64.tar.gz": (
            "python/bin/python3.12",
            "python/lib/python3.12/LICENSE.txt",
        ),
        "cpython-3.12.13+20260510-macos-arm64.tar.gz": (
            "python/bin/python3.12",
            "python/lib/python3.12/LICENSE.txt",
        ),
    }

    for archive_name, (executable, license_file) in expected.items():
        with tarfile.open(ROOT / "runtime" / "archives" / archive_name, "r:gz") as archive:
            executable_info = archive.getmember(executable)
            assert archive.getmember(license_file).isfile()
            assert executable_info.isfile()
            if "/bin/" in executable:
                assert executable_info.mode & 0o111


def test_hooks_use_only_the_portable_runtime_launcher() -> None:
    # Given: the installed hook command configuration.
    hooks = (ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")

    # When / Then: no system Python command remains at the bootstrap boundary.
    assert "python3 " not in hooks
    assert "py -3 " not in hooks
    assert hooks.count("launch-python") == 2
    assert hooks.count("-ForwardStdin") == 1


def test_every_script_launcher_enables_cpython_isolated_mode() -> None:
    powershell = (ROOT / "runtime" / "launch-python.ps1").read_text(encoding="utf-8")
    posix = (ROOT / "runtime" / "launch-python.sh").read_text(encoding="utf-8")
    rust = (ROOT / "runtime" / "windows-launcher" / "src" / "main.rs").read_text(encoding="utf-8")
    generated = (ROOT / "scripts" / "installer_mcp_runtime.py").read_text(encoding="utf-8")

    assert powershell.count("-I -B") == 2
    assert posix.count("-I -B") == 3
    assert '.arg("-I")' in rust
    assert '.arg("-B")' in rust
    assert 'python3" -I -B "$@"' in generated


def test_hooks_register_only_low_frequency_lifecycle_events() -> None:
    hooks = cast(
        "dict[str, object]",
        json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")),
    )
    registered = cast("dict[str, object]", hooks["hooks"])

    assert set(registered) == {"UserPromptSubmit"}


def test_only_explicit_work_on_prompt_hook_is_registered() -> None:
    hooks = (ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")

    assert "session_hook.py" not in hooks
    assert hooks.count("work_on_hook.py") == 2
    assert "hook_event.py" not in hooks


def test_machine_entrypoints_are_packaged_and_import_reachable() -> None:
    manifest = cast(
        "list[object]",
        json.loads((ROOT / "runtime" / "package-files.json").read_text(encoding="utf-8")),
    )
    packaged = {value for value in manifest if isinstance(value, str)}
    hooks = (ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    hook_scripts = set(re.findall(r"\$\{PLUGIN_ROOT\}/(scripts/[a-z_]+\.py)", hooks))
    mcp = cast(
        "dict[str, object]",
        json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8")),
    )
    servers = cast("dict[str, object]", mcp["mcpServers"])
    server = cast("dict[str, object]", servers["codex-must-work"])
    arguments = cast("list[object]", server["args"])
    mcp_scripts = {value for value in arguments if isinstance(value, str) and value.endswith(".py")}

    assert hook_scripts | mcp_scripts <= packaged
    assert {
        module.removeprefix("scripts.").replace(".", "/") for module in ALLOWED_SCRIPTS_MODULES
    } <= {
        path.removeprefix("scripts/").removesuffix(".py")
        for path in packaged
        if path.startswith("scripts/") and path.endswith(".py")
    }
