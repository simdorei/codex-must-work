# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# ─── How to run ───
# uv run python tests/check_commit_contract.py --base <sha> --head <sha>
# uv run python tests/check_commit_contract.py --self-test

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.commit_contract_paths import RULES  # noqa: E402
from tests.commit_contract_rules import check_contract  # noqa: E402

_git_executable = shutil.which("git")
if _git_executable is None:
    _message = "git executable is required"
    raise RuntimeError(_message)
GIT: Final = _git_executable


class _Arguments(argparse.Namespace):
    base: str | None = None
    head: str | None = None
    remote_ref: str | None = None
    self_test: bool = False


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 -- absolute Git and fixed self-test arguments only
        (GIT, *arguments),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    ).stdout.strip()


def _write(repo: Path, relative: str, contents: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(contents, encoding="utf-8", newline="\n")


def _commit(repo: Path, subject: str) -> None:
    _ = _git(repo, "add", "--all")
    _ = _git(repo, "commit", "-q", "-m", subject)


def _base(repo: Path) -> str:
    _ = _git(repo, "init", "-q", "-b", "main")
    _ = _git(repo, "config", "user.name", "CMW Contract Self Test")
    _ = _git(repo, "config", "user.email", "cmw-contract@example.invalid")
    marketplace = {
        "name": "simdorei",
        "plugins": [
            {
                "name": "codex-must-work",
                "source": {"source": "local", "path": "./"},
            }
        ],
    }
    manifest = {
        "name": "codex-must-work",
        "version": "0.1.0+codex.20260720221156",
        "description": "fixture",
    }
    _write(repo, ".agents/plugins/marketplace.json", json.dumps(marketplace, indent=2) + "\n")
    _write(repo, ".codex-plugin/plugin.json", json.dumps(manifest, indent=2) + "\n")
    _write(repo, "README.md", "# Fixture\n\n## Installation\n\nold\n\n## Usage\n\nsame\n")
    structured = {
        ".agents/plugins/marketplace.json",
        ".codex-plugin/plugin.json",
        "README.md",
        "tests/test_hook_event.py",
    }
    seen: set[str] = set()
    modified_at_base: set[str] = set()
    for rule in RULES:
        modified_at_base.update(rule.modified - seen)
        seen.update(rule.paths)
    for path in sorted(modified_at_base - structured):
        _write(repo, path, f"base:{path}\n")
    _write(
        repo,
        "tests/test_hook_event.py",
        "from pathlib import Path\n\n\ndef test_existing() -> None:\n    assert Path('.')\n",
    )
    _commit(repo, "base")
    return _git(repo, "rev-parse", "HEAD")


def _ordinary_commit(repo: Path, index: int) -> None:
    rule = RULES[index]
    for path in sorted(rule.paths - {"tests/test_hook_event.py"}):
        _write(repo, path, f"fixture:{index}:{path}\n")
    if index == 6:
        lines = (
            "import pytest",
            "from pathlib import Path",
            "",
            "_EVENT = 'Stop'",
            "",
            "",
            "def test_new() -> None:",
            "    assert _EVENT",
            "",
            "",
            "def test_existing() -> None:",
            "    assert Path('.')",
            "",
        )
        contents = "\n".join(lines)
        _write(
            repo,
            "tests/test_hook_event.py",
            contents,
        )


def _metadata_commit(repo: Path, disallowed_hunk: bool) -> None:
    rule = RULES[5]
    for path in sorted(rule.paths - rule.modified):
        _write(repo, path, f"fixture:{path}\n")
    marketplace = {
        "name": "codex-must-work-local",
        "interface": {"displayName": "Codex Must Work Local"},
        "plugins": [
            {
                "name": "codex-must-work",
                "source": {"source": "local", "path": "./"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Developer Tools",
            }
        ],
    }
    manifest = {
        "name": "codex-must-work",
        "version": "0.2.0",
        "description": "changed" if disallowed_hunk else "fixture",
        "hooks": ["./hooks/hooks.json"],
    }
    _write(repo, ".agents/plugins/marketplace.json", json.dumps(marketplace, indent=2) + "\n")
    _write(repo, ".codex-plugin/plugin.json", json.dumps(manifest, indent=2) + "\n")
    _write(repo, "README.md", "# Fixture\n\n## Installation\n\nnew\n\n## Usage\n\nsame\n")


def _history(repo: Path, mutation: str) -> tuple[str, str]:
    base = _base(repo)
    count = len(RULES) - 1 if mutation == "wrong_count" else len(RULES)
    for index in range(count):
        if index == 5:
            _metadata_commit(repo, mutation == "disallowed_hunk")
        else:
            _ordinary_commit(repo, index)
        if index == 0 and mutation == "cross_path":
            _write(repo, "unexpected.txt", "not owned\n")
        subject = RULES[index].subject
        if index == 1 and mutation == "wrong_subject":
            subject = "feat(installer): wrong synthetic subject"
        _commit(repo, subject)
    return base, _git(repo, "rev-parse", "HEAD")


def _self_test() -> int:
    cases = {
        "valid": None,
        "wrong_count": "wrong_commit_count",
        "wrong_subject": "wrong_subject",
        "cross_path": "cross_owned_path",
        "disallowed_hunk": "disallowed_json_pointer",
    }
    for mutation, expected in cases.items():
        with TemporaryDirectory(prefix="cmw-commit-contract.") as temporary:
            repo = Path(temporary)
            base, head = _history(repo, mutation)
            errors = check_contract(repo, base, head)
        if expected is None and errors:
            _ = sys.stderr.write(f"self_test_failed:{mutation}:{errors}\n")
            return 1
        if expected is not None and not any(error.startswith(expected) for error in errors):
            _ = sys.stderr.write(f"self_test_failed:{mutation}:{errors}\n")
            return 1
    _ = sys.stdout.write("self_test=ok\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--base")
    _ = parser.add_argument("--head")
    _ = parser.add_argument("--remote-ref")
    _ = parser.add_argument("--self-test", action="store_true")
    arguments = _Arguments()
    _ = parser.parse_args(argv, namespace=arguments)
    if arguments.self_test:
        if (
            arguments.base is not None
            or arguments.head is not None
            or arguments.remote_ref is not None
        ):
            parser.error("--self-test cannot be combined with history arguments")
        return _self_test()
    if arguments.base is None or arguments.head is None:
        parser.error("--base and --head are required unless --self-test is used")
    errors = check_contract(ROOT, arguments.base, arguments.head, arguments.remote_ref)
    if errors:
        for error in errors:
            _ = sys.stderr.write(f"{error}\n")
        return 1
    _ = sys.stdout.write("commit_contract=ok\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
