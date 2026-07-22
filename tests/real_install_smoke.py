from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import subprocess


class AbsolutePathError(argparse.ArgumentTypeError):
    def __init__(self) -> None:
        super().__init__("path must be absolute")


class AbsoluteRuntimeError(ValueError):
    def __init__(self) -> None:
        super().__init__("selected Codex runtime must be absolute")


@dataclass(frozen=True, slots=True)
class Arguments:
    codex_home: Path
    source_root: Path
    expected_source_head: str
    expected_source_tree: str
    verify_idempotent_reinstall: bool


class Runner(Protocol):
    def __call__(
        self, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]: ...


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise AbsolutePathError
    return path


def parse_args(argv: list[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--codex-home", required=True, type=_absolute)
    _ = parser.add_argument("--source-root", required=True, type=_absolute)
    _ = parser.add_argument("--expected-source-head", required=True)
    _ = parser.add_argument("--expected-source-tree", required=True)
    _ = parser.add_argument("--verify-idempotent-reinstall", required=True, action="store_true")
    namespace = parser.parse_args(argv)
    return Arguments(
        namespace.codex_home,
        namespace.source_root,
        namespace.expected_source_head,
        namespace.expected_source_tree,
        namespace.verify_idempotent_reinstall,
    )


def run_codex(
    executable: Path,
    codex_home: Path,
    runner: Runner,
    parent_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not executable.is_absolute():
        raise AbsoluteRuntimeError
    environment = dict(os.environ if parent_environment is None else parent_environment)
    environment["CODEX_HOME"] = str(codex_home)
    prompt = (
        "이 문장을 영어로 이해하고 첫 줄에 번역 안내를 유지한 뒤 "
        "마지막 줄에 SMOKE_OK를 쓰세요."
    )
    return runner((str(executable), "exec", "--json", prompt), environment)
