from __future__ import annotations

import io
import os
import subprocess
from typing import TYPE_CHECKING, Final

import pytest

from tests import staged_secret_scan_git as scanner_git
from tests.test_staged_secret_scan import PYTHON, ROOT, git, joined, repo, write_stage

if TYPE_CHECKING:
    from pathlib import Path

READER_DETAIL: Final[str] = "private reader failure detail"


class _BrokenPipe:
    def read(self, _size: int = -1) -> bytes:
        raise OSError(READER_DETAIL)

    def close(self) -> None:
        return


class _FakeProcess:
    stdout: _BrokenPipe
    stderr: io.BytesIO
    returncode: int

    def __init__(self) -> None:
        self.stdout = _BrokenPipe()
        self.stderr = io.BytesIO()
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return self.returncode

    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return


def test_replace_ref_and_hostile_git_environment_cannot_hide_staged_secret(
    tmp_path: Path,
) -> None:
    path = repo(tmp_path)
    relative = "scripts/hidden.py"
    write_stage(
        path,
        relative,
        joined("API_KEY = 'sk-", "live-12345678901234567890'\n"),
    )
    head = git(path, "rev-parse", "HEAD").stdout.strip()
    staged_tree = git(path, "write-tree").stdout.strip()
    replacement = git(
        path,
        "commit-tree",
        staged_tree,
        "-p",
        head,
        "-m",
        "replacement",
    ).stdout.strip()
    _ = git(path, "replace", head, replacement)
    raw_diff = git(
        path,
        "--no-replace-objects",
        "diff-tree",
        "-r",
        "--name-only",
        "--no-commit-id",
        f"{head}^{{tree}}",
        staged_tree,
    )
    environment = os.environ.copy()
    hostile_objects = tmp_path / "hostile-objects"
    hostile_objects.mkdir()
    environment.update(
        {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(hostile_objects),
            "GIT_NO_REPLACE_OBJECTS": "0",
            "GIT_OBJECT_DIRECTORY": str(hostile_objects),
            "GIT_REPLACE_REF_BASE": "refs/replace/",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.useReplaceRefs",
            "GIT_CONFIG_VALUE_0": "true",
        }
    )
    result = subprocess.run(  # noqa: S603
        (str(PYTHON), str(ROOT / "tests" / "staged_secret_scan.py"), "--cached", "--redact"),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert relative in raw_diff.stdout.splitlines()
    assert result.returncode == 1
    assert result.stdout.strip() == f"SECRET_OPENAI_TOKEN {relative}"
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("head_contents", "expected_rule"),
    [
        (f"{'f' * 40}\n", "HEAD_UNREADABLE ."),
        ("not-a-valid-head\n", "NOT_REPOSITORY ."),
    ],
)
def test_zero_ref_detached_or_malformed_head_fails_closed(
    tmp_path: Path,
    head_contents: str,
    expected_rule: str,
) -> None:
    path = repo(tmp_path)
    write_stage(path, "scripts/safe.py", "print('safe')\n")
    (path / ".git" / "refs" / "heads" / "main").unlink()
    assert git(path, "for-each-ref").stdout == ""
    _ = (path / ".git" / "HEAD").write_text(
        head_contents,
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(  # noqa: S603
        (str(PYTHON), str(ROOT / "tests" / "staged_secret_scan.py"), "--cached", "--redact"),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    assert result.stdout.strip() == expected_rule
    assert result.stderr == ""


def test_true_unborn_symbolic_head_uses_empty_tree(tmp_path: Path) -> None:
    path = repo(tmp_path)
    _ = git(path, "symbolic-ref", "HEAD", "refs/heads/release")
    write_stage(path, "scripts/safe.py", "print('safe')\n")
    result = subprocess.run(  # noqa: S603
        (str(PYTHON), str(ROOT / "tests" / "staged_secret_scan.py"), "--cached", "--redact"),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert "staged_files=2 scanned_blobs=2 findings=0" in result.stdout
    assert result.stderr == ""


def test_malformed_nested_git_does_not_fall_back_to_parent_repository(
    tmp_path: Path,
) -> None:
    parent = repo(tmp_path)
    child = parent / "nested-source"
    _ = (child / ".git").mkdir(parents=True)
    _ = (child / ".git" / "HEAD").write_text(
        "not-a-valid-head\n",
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(  # noqa: S603
        (str(PYTHON), str(ROOT / "tests" / "staged_secret_scan.py"), "--cached", "--redact"),
        cwd=child,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    assert result.stdout.strip() == "NOT_REPOSITORY ."
    assert result.stderr == ""


def test_non_repository_nested_source_does_not_fall_back_to_parent_repository(
    tmp_path: Path,
) -> None:
    parent = repo(tmp_path)
    child = parent / "plain-source"
    _ = child.mkdir()
    result = subprocess.run(  # noqa: S603
        (str(PYTHON), str(ROOT / "tests" / "staged_secret_scan.py"), "--cached", "--redact"),
        cwd=child,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    assert result.stdout.strip() == "NOT_REPOSITORY ."
    assert result.stderr == ""


def test_untrusted_per_call_git_environment_is_rejected(tmp_path: Path) -> None:
    path = repo(tmp_path)
    with pytest.raises(scanner_git.ScanError) as caught:
        _ = scanner_git.run_git(
            path,
            "status",
            environment={"GIT_REPLACE_REF_BASE": "refs/evil/"},
        )
    assert caught.value.rule == scanner_git.GIT_EXECUTION_ERROR


def test_reader_thread_failure_reaches_redacted_main_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = repo(tmp_path)

    def fake_popen(
        *_args: str | tuple[str, ...],
        **_kwargs: str | Path | dict[str, str] | int | None,
    ) -> _FakeProcess:
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(scanner_git.ScanError) as caught:
        _ = scanner_git.run_git(path, "status")
    assert caught.value.rule == scanner_git.GIT_EXECUTION_ERROR
    assert READER_DETAIL not in str(caught.value)
