from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Final, cast

from tests.commit_contract_paths import RULES, CommitRule

if TYPE_CHECKING:
    from pathlib import Path

GIT: Final = shutil.which("git")
if GIT is None:
    msg = "git executable is required"
    raise RuntimeError(msg)


def check_contract(
    repo: Path, base: str, head: str, remote_ref: str | None = None
) -> tuple[str, ...]:
    errors: list[str] = []
    if _git(repo, "merge-base", "--is-ancestor", base, head, check=False).returncode:
        return ("base_is_not_ancestor",)
    rows = _git(repo, "log", "--reverse", "--format=%H%x09%s", f"{base}..{head}").stdout
    commits = [row.split("\t", 1) for row in rows.splitlines() if row]
    if len(commits) != len(RULES):
        errors.append(f"wrong_commit_count:{len(commits)}")
    for index, (commit, subject) in enumerate(commits[: len(RULES)]):
        rule = RULES[index]
        if subject != rule.subject:
            errors.append(f"wrong_subject:{index + 1}:{subject}")
        errors.extend(_check_paths(repo, commit, rule, index + 1))
    if len(commits) == len(RULES):
        errors.extend(_check_metadata_hunks(repo, commits[5][0]))
        errors.extend(_check_hook_test_hunk(repo, commits[6][0]))
    if remote_ref is not None:
        remote = _git(repo, "rev-parse", "--verify", remote_ref, check=False)
        if remote.returncode or remote.stdout.strip() != head:
            errors.append("remote_ref_mismatch")
    return tuple(errors)


def _check_paths(repo: Path, commit: str, rule: CommitRule, number: int) -> tuple[str, ...]:
    output = _git(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--name-status",
        "-r",
        "--no-renames",
        commit,
    ).stdout
    observed: dict[str, str] = {}
    for row in output.splitlines():
        status, path = row.split("\t", 1)
        observed[path] = status
    errors: list[str] = []
    if set(observed) != set(rule.paths):
        extra = sorted(set(observed) - rule.paths)
        missing = sorted(rule.paths - set(observed))
        errors.append(f"cross_owned_path:{number}:extra={extra}:missing={missing}")
    for path, status in observed.items():
        expected = "M" if path in rule.modified else "A"
        if status != expected:
            errors.append(f"wrong_path_status:{number}:{path}:{status}")
    return tuple(errors)


def _check_metadata_hunks(repo: Path, commit: str) -> tuple[str, ...]:
    errors: list[str] = []
    old_market = _json_blob(repo, f"{commit}^", ".agents/plugins/marketplace.json")
    new_market = _json_blob(repo, commit, ".agents/plugins/marketplace.json")
    for document in (old_market, new_market):
        _ = document.pop("name", None)
        _ = document.pop("interface", None)
        plugins = cast("list[dict[str, object]]", document["plugins"])
        _ = plugins[0].pop("policy", None)
        _ = plugins[0].pop("category", None)
    if old_market != new_market:
        errors.append("disallowed_json_pointer:.agents/plugins/marketplace.json")
    old_manifest = _json_blob(repo, f"{commit}^", ".codex-plugin/plugin.json")
    new_manifest = _json_blob(repo, commit, ".codex-plugin/plugin.json")
    for document in (old_manifest, new_manifest):
        _ = document.pop("version", None)
        _ = document.pop("hooks", None)
    if old_manifest != new_manifest:
        errors.append("disallowed_json_pointer:.codex-plugin/plugin.json")
    allowed_headings = {"## Installation", "## 설치 후 확인"}
    for side, lines in _changed_readme_lines(repo, commit):
        source = _blob(repo, f"{commit}^" if side == "old" else commit, "README.md").splitlines()
        errors.extend(
            f"disallowed_readme_heading:{side}:{line}"
            for line in lines
            if _heading_at(source, line) not in allowed_headings
        )
    return tuple(errors)


def _check_hook_test_hunk(repo: Path, commit: str) -> tuple[str, ...]:
    old_tree = ast.parse(_blob(repo, f"{commit}^", "tests/test_hook_event.py"))
    new_tree = ast.parse(_blob(repo, commit, "tests/test_hook_event.py"))
    old_tests = _named_nodes(old_tree, ast.FunctionDef, "test_")
    new_tests = _named_nodes(new_tree, ast.FunctionDef, "test_")
    old_assignments = _assignments(old_tree)
    new_assignments = _assignments(new_tree)
    if any(name not in new_tests or new_tests[name] != node for name, node in old_tests.items()):
        return ("disallowed_hook_event_hunk:existing_test_changed",)
    if any(
        name not in new_assignments or new_assignments[name] != node
        for name, node in old_assignments.items()
    ):
        return ("disallowed_hook_event_hunk:existing_fixture_changed",)
    if any(not name.startswith("_") for name in new_assignments.keys() - old_assignments.keys()):
        return ("disallowed_hook_event_hunk:new_public_fixture",)
    if _other_nodes(old_tree) != _other_nodes(new_tree):
        return ("disallowed_hook_event_hunk:non_import_or_test",)
    return ()


def _changed_readme_lines(repo: Path, commit: str) -> tuple[tuple[str, range], ...]:
    patch = _git(repo, "show", "--format=", "--unified=0", commit, "--", "README.md").stdout
    ranges: list[tuple[str, range]] = []
    pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
    for matched in pattern.finditer(patch):
        old_start, old_count, new_start, new_count = matched.groups()
        old_size = int(old_count or "1")
        new_size = int(new_count or "1")
        ranges.extend(
            (
                ("old", range(int(old_start), int(old_start) + old_size)),
                ("new", range(int(new_start), int(new_start) + new_size)),
            )
        )
    return tuple(ranges)


def _heading_at(lines: list[str], line: int) -> str | None:
    for value in reversed(lines[:line]):
        if value.startswith("## "):
            return value
    return None


def _named_nodes(tree: ast.Module, kind: type[ast.FunctionDef], prefix: str) -> dict[str, str]:
    return {
        node.name: ast.dump(node, include_attributes=False)
        for node in tree.body
        if isinstance(node, kind) and node.name.startswith(prefix)
    }


def _assignments(tree: ast.Module) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            values[node.targets[0].id] = ast.dump(node, include_attributes=False)
    return values


def _other_nodes(tree: ast.Module) -> tuple[str, ...]:
    return tuple(
        ast.dump(node, include_attributes=False)
        for node in tree.body
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign))
        and not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))
    )


def _json_blob(repo: Path, revision: str, path: str) -> dict[str, object]:
    value = cast("object", json.loads(_blob(repo, revision, path)))
    if not isinstance(value, dict):
        msg = f"expected JSON object: {path}"
        raise TypeError(msg)
    return cast("dict[str, object]", value)


def _blob(repo: Path, revision: str, path: str) -> str:
    return _git(repo, "show", f"{revision}:{path}").stdout


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- absolute Git and fixed checker arguments only
        (GIT, *arguments),
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
