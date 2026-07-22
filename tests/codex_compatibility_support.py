from __future__ import annotations

import json
import os
import subprocess
import tomllib
from base64 import b64encode
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import codex_compatibility
from scripts import codex_compatibility_policy as policy_module
from scripts.codex_compatibility import (
    ALLOWED_CODEX_RELEASES,
    CompatibilityResult,
    validate_codex_compatibility,
)
from scripts.codex_compatibility_policy import (
    MANAGED_SOURCE_KIND_ORDER,
    MANAGED_SOURCE_KINDS_BY_RELEASE,
    MANAGED_SOURCE_ORDER,
    MANAGED_SOURCE_SEARCH_BY_RELEASE,
    PolicySourceSpec,
)
from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from collections.abc import Callable

ALLOWED = {
    "0.144.0-alpha.4": "049586f41571e74b44c841868bca3a2233214a71",
    "0.144.0": "767822446c7a594caa19609ca435281a9ec67e0d",
    "0.145.0-alpha.18": "f84f9a6406cc55b210395f71b4c6aed236fc7ebb",
}


def binary_names() -> tuple[str, str]:
    suffix = ".exe" if os.name == "nt" else ""
    return f"codex{suffix}", f"codex-code-mode-host{suffix}"


def bundle_fixture(home: Path, relative: str, *, host: bool = True) -> Path:
    codex_name, host_name = binary_names()
    root = home / relative
    root.mkdir(parents=True)
    executable = root / codex_name
    _ = executable.write_bytes(b"codex-fixture")
    if host:
        _ = (root / host_name).write_bytes(b"host-fixture")
    return executable.resolve()


def source_fixture(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / ".agents" / "plugins").mkdir(parents=True)
    _ = (source / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "codex-must-work-local",
                "plugins": [
                    {"name": "codex-must-work", "source": {"source": "local", "path": "./"}}
                ],
            }
        ),
        encoding="utf-8",
    )
    return source.resolve()


def fake_commands(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "0.144.0",
    hooks: bool = True,
    plugins: bool = True,
) -> list[tuple[tuple[str, ...], Path | None, dict[str, str]]]:
    calls: list[tuple[tuple[str, ...], Path | None, dict[str, str]]] = []

    def run(
        argv: tuple[str, ...], *, env: dict[str, str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd, env))
        if argv[1:] == ("--version",):
            return subprocess.CompletedProcess(argv, 0, f"codex-cli {version}\n", "")
        if argv[1:] == ("features", "list"):
            output = (
                f"hooks stable {str(hooks).lower()}\nplugins experimental {str(plugins).lower()}\n"
            )
            return subprocess.CompletedProcess(argv, 0, output, "")
        assert argv[1:] == (
            "plugin",
            "list",
            "--available",
            "--json",
            "--marketplace",
            "codex-must-work-local",
        )
        assert cwd is None
        temporary_home = Path(env["CODEX_HOME"])
        parsed = tomllib.loads((temporary_home / "config.toml").read_text(encoding="utf-8"))
        assert parsed["notice"] == {
            "hide_world_writable_warning": True,
            "hide_full_access_warning": True,
        }
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {
                        "name": "codex-must-work",
                        "marketplace": "codex-must-work-local",
                        "source": {"source": "local", "path": "./"},
                    }
                ]
            ),
            "",
        )

    monkeypatch.setattr(codex_compatibility, "_run_command", run)
    return calls


def policy_specs(path: Path | None, *, opaque: bool = False) -> tuple[PolicySourceSpec, ...]:
    kinds = dict(MANAGED_SOURCE_KIND_ORDER)
    return tuple(
        PolicySourceSpec(
            name=name,
            path=path if name == "cloud_requirements" else None,
            opaque_when_present=opaque and name == "cloud_requirements",
            source_kind=kinds[name],
        )
        for name in MANAGED_SOURCE_ORDER
    )


def policy_spec_provider(
    path: Path | None, *, opaque: bool = False
) -> Callable[[Path, str], tuple[PolicySourceSpec, ...]]:
    def provider(_home: Path, _version: str) -> tuple[PolicySourceSpec, ...]:
        return policy_specs(path, opaque=opaque)

    return provider


def cloud_cache(*, config: str = "[features]\nhooks = true\n", requirements: str = "") -> bytes:
    def fragments(contents: str) -> list[dict[str, str]]:
        return [] if not contents else [{"id": "one", "name": "managed", "contents": contents}]

    return json.dumps(
        {
            "signed_payload": {
                "version": 1,
                "cached_at": "2026-07-22T00:00:00Z",
                "expires_at": "2026-07-22T01:00:00Z",
                "chatgpt_user_id": "redacted",
                "account_id": "redacted",
                "bundle": {
                    "config_toml": {"enterprise_managed": fragments(config)},
                    "requirements_toml": {"enterprise_managed": fragments(requirements)},
                },
            },
            "signature": "opaque",
        },
        separators=(",", ":"),
    ).encode()

