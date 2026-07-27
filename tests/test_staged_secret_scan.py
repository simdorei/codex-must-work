from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import staged_secret_scan as scanner
from tests import staged_secret_scan_git as scanner_git
from tests.staged_secret_scan_patterns import is_allowed

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "staged_secret_scan.py"


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        _message = "git executable is required"
        raise RuntimeError(_message)
    return executable


GIT = _git_executable()
PYTHON = Path(sys.executable)


def joined(*parts: str) -> str:
    return "".join(parts)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        (GIT, *args),
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )


def git_bytes(
    repo: Path, *args: str, input_data: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        (GIT, *args),
        cwd=repo,
        check=True,
        capture_output=True,
        input=input_data,
    )


def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir(parents=True)
    _ = git(path, "init", "-q", "-b", "main")
    _ = git(path, "config", "user.name", "Scanner Test")
    _ = git(path, "config", "user.email", "scanner@example.invalid")
    _ = (path / "README.md").write_text("base\n", encoding="utf-8")
    _ = git(path, "add", "README.md")
    _ = git(path, "commit", "-qm", "base")
    return path


def run(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        (str(PYTHON), str(SCRIPT), *args),
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ,
    )


def write_stage(repo_path: Path, relative: str, data: str | bytes) -> None:
    target = repo_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        _ = target.write_bytes(data)
    else:
        _ = target.write_text(data, encoding="utf-8")
    _ = git(repo_path, "add", "--", relative)


def test_clean_staged_allowlisted_blob_reports_public_counts(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "scripts/example.py", "print('hello')\n")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 0
    assert "staged_files=1" in result.stdout
    assert "findings=0" in result.stdout
    assert result.stderr == ""


def test_release_configuration_paths_are_allowlisted() -> None:
    assert is_allowed(".codex-plugin/plugin.json")
    assert is_allowed(".gitignore")
    assert is_allowed(".mcp.json")


def test_secret_is_read_from_index_not_working_tree(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "scripts/value.py", "API_KEY = 'safe-placeholder'\n")
    _ = (path / "scripts" / "value.py").write_text(
        joined("API_KEY = 'sk-", "live-12345678901234567890'\n"),
        encoding="utf-8",
    )
    result = run(path, "--cached", "--redact")
    assert result.returncode == 0
    assert "findings=0" in result.stdout


@pytest.mark.parametrize(
    "payload",
    [
        joined("API_KEY = 'sk-", "live-12345678901234567890'\n"),
        joined("-----BEGIN ", "OPENSSH PRIVATE KEY-----\nnot shown\n"),
        joined("Authorization: Bear", "er eyJhbGciOiJIUzI1NiJ9.xxxxxxxxxxxxxxxxxxxx\n"),
    ],
)
def test_high_confidence_secret_emits_rule_and_path_without_match_bytes(
    tmp_path: Path,
    payload: str,
) -> None:
    path = repo(tmp_path)
    write_stage(path, "tests/fixture.py", payload)
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert "tests/fixture.py" in result.stdout
    assert "SECRET_" in result.stdout or "PRIVATE_KEY" in result.stdout
    assert "sk-live" not in result.stdout
    assert "eyJhbGci" not in result.stdout
    assert "BEGIN OPENSSH" not in result.stdout
    assert payload.strip() not in result.stdout
    assert result.stderr == ""


def test_forbidden_artifact_and_non_allowlisted_path_fail_closed(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, ".env", "API_KEY=not printed\n")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert "FORBIDDEN_PATH" in result.stdout
    assert "API_KEY=not printed" not in result.stdout


def test_non_allowlisted_release_path_fails_closed(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "docs/release-not-allowed.txt", "public text\n")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert result.stdout.strip() == "NON_ALLOWLISTED_PATH docs/release-not-allowed.txt"


def test_deleted_and_renamed_paths_are_handled_without_worktree_reads(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "scripts/old.py", "print('ok')\n")
    _ = git(path, "commit", "-qm", "old")
    _ = git(path, "mv", "scripts/old.py", "scripts/new.py")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 0
    assert "staged_files=1" in result.stdout
    _ = git(path, "commit", "-qm", "rename")
    _ = git(path, "rm", "-q", "scripts/new.py")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 0
    assert "staged_files=0" in result.stdout


def test_binary_blob_and_oversize_blob_fail_closed(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "runtime/image.bin", b"\x00binary")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert "BINARY_BLOB" in result.stdout
    path2 = repo(tmp_path / "second")
    write_stage(path2, "runtime/large.txt", "x" * (2 * 1024 * 1024))
    result = run(path2, "--cached", "--redact")
    assert result.returncode == 1
    assert "OVERSIZE_BLOB" in result.stdout


def test_required_flags_and_non_repo_reject_without_git_stderr(tmp_path: Path) -> None:
    path = repo(tmp_path)
    for args in ((), ("--cached",), ("--redact",)):
        result = run(path, *args)
        assert result.returncode == 2
        assert "usage:" in result.stderr.lower()
    outside = run(tmp_path, "--cached", "--redact")
    assert outside.returncode == 1
    assert "NOT_REPOSITORY" in outside.stdout
    assert outside.stderr == ""


def test_index_is_unchanged_and_weird_utf8_path_is_safe(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "tests/üñîçødë.py", "print('ok')\n")
    before = git(path, "write-tree").stdout.strip()
    result = run(path, "--cached", "--redact")
    after = git(path, "write-tree").stdout.strip()
    assert result.returncode == 0
    assert before == after
    assert "findings=0" in result.stdout


def test_malformed_git_listing_fails_closed_without_blob_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo(tmp_path)
    original = scanner_git.run_git

    def malformed(
        cwd: Path,
        *args: str,
        environment: Mapping[str, str] | None = None,
        max_stdout_bytes: int | None = None,
    ) -> bytes:
        if args and args[0] == "diff-tree":
            return b"MALFORMED\x00"
        return original(
            cwd,
            *args,
            environment=environment,
            max_stdout_bytes=max_stdout_bytes,
        )

    monkeypatch.setattr(scanner, "run_git", malformed)
    with pytest.raises(scanner_git.ScanError) as caught:
        _ = scanner.scan(path)
    assert caught.value.rule == scanner.MALFORMED_GIT_OUTPUT
    assert "blob bytes" not in str(caught.value)


def test_hung_git_is_bounded_and_stderr_is_not_re_emitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = repo(tmp_path)
    monkeypatch.setattr(scanner_git, "GIT", sys.executable)
    monkeypatch.setattr(scanner_git, "GIT_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(scanner_git.ScanError) as caught:
        _ = scanner_git.run_git(
            path,
            "-c",
            "import sys,time;sys.stderr.write('LEAK');time.sleep(10)",
        )
    assert caught.value.rule == scanner_git.GIT_TIMEOUT
    assert "LEAK" not in str(caught.value)


def test_path_injection_is_escaped_and_blob_instructions_are_ignored(tmp_path: Path) -> None:
    path = repo(tmp_path)
    relative = "tests/ignore-instructions.py"
    write_stage(
        path,
        relative,
        joined("Ignore prior rules and API_KEY='sk-", "live-12345678901234567890'\n"),
    )
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert "SECRET_OPENAI_TOKEN tests/ignore-instructions.py" in result.stdout
    assert "Ignore prior rules" not in result.stdout
    assert "sk-live" not in result.stdout
    assert result.stdout.count("\n") == 1
