"""Render the trust-aware installer command result."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from scripts.installer_result import InstallResult

_CLI_ARGUMENT_COUNT: Final = 2


class _Installer(Protocol):
    def __call__(self, codex_home: Path, source_root: Path) -> InstallResult: ...


def run_cli(installer: _Installer, argv: list[str] | None = None) -> int:
    """Run the two-path installer command."""
    values = sys.argv[1:] if argv is None else argv
    if len(values) != _CLI_ARGUMENT_COUNT:
        _ = sys.stderr.write("usage: install_plugin.py CODEX_HOME SOURCE_ROOT\n")
        return 2
    result = installer(Path(values[0]), Path(values[1]))
    if result.install_ok:
        _ = sys.stdout.write("install=ok\n")
        return 0
    _ = sys.stderr.write(json.dumps(asdict(result), sort_keys=True) + "\n")
    return 1
