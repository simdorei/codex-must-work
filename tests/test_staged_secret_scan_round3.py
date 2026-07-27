from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import staged_secret_scan as scanner
from tests import staged_secret_scan_git as scanner_git
from tests.test_staged_secret_scan import PYTHON, ROOT, git, repo, write_stage

if TYPE_CHECKING:
    from collections.abc import Mapping

OWNED_FILES = (
    "tests/staged_secret_scan.py",
    "tests/staged_secret_scan_git.py",
    "tests/staged_secret_scan_index.py",
    "tests/staged_secret_scan_patterns.py",
    "tests/staged_secret_scan_snapshot.py",
    "tests/test_staged_secret_scan.py",
    "tests/test_staged_secret_scan_adversarial.py",
    "tests/test_staged_secret_scan_round3.py",
    "tests/test_staged_secret_scan_round4.py",
)


def _index_sha(repo_path: Path) -> str:
    return hashlib.sha256((repo_path / ".git" / "index").read_bytes()).hexdigest()


def _object_state(repo_path: Path) -> tuple[tuple[str, str], ...]:
    objects = repo_path / ".git" / "objects"
    return tuple(
        sorted(
            (
                path.relative_to(objects).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in objects.rglob("*")
            if path.is_file()
        )
    )


def test_exact_owned_release_files_self_scan_clean(tmp_path: Path) -> None:
    path = repo(tmp_path)
    for relative in OWNED_FILES:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(ROOT / relative, target)
    _ = git(path, "add", "--", *OWNED_FILES)
    snapshot_id = _index_sha(path)
    objects_before = _object_state(path)
    result = subprocess.run(  # noqa: S603
        (str(PYTHON), str(path / "tests" / "staged_secret_scan.py"), "--cached", "--redact"),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert result.stdout == (
        f"OK staged_files=9 scanned_blobs=9 findings=0 snapshot={snapshot_id}\n"
    )
    assert result.stderr == ""
    assert _index_sha(path) == snapshot_id
    assert _object_state(path) == objects_before


def test_scan_preserves_real_index_and_object_database_bytes(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "scripts/safe.py", "print('safe')\n")
    index_before = _index_sha(path)
    objects_before = _object_state(path)
    result = subprocess.run(  # noqa: S603
        (str(PYTHON), str(ROOT / "tests" / "staged_secret_scan.py"), "--cached", "--redact"),
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert _index_sha(path) == index_before
    assert _object_state(path) == objects_before


def test_corrupt_head_fails_closed(tmp_path: Path) -> None:
    path = repo(tmp_path)
    write_stage(path, "scripts/safe.py", "print('safe')\n")
    _ = (path / ".git" / "HEAD").write_text(
        "ref: refs/tags/missing\n", encoding="utf-8", newline="\n"
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
    assert result.stdout.strip() == "HEAD_UNREADABLE ."
    assert result.stderr == ""


def test_git_output_cap_uses_streaming_child_not_capture_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo(tmp_path)

    def forbidden_run(*_args: str, **_kwargs: str) -> None:
        pytest.fail("subprocess.run materializes unbounded output")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    monkeypatch.setattr(scanner_git, "GIT", sys.executable)
    monkeypatch.setattr(scanner_git, "MAX_GIT_OUTPUT_BYTES", 1024)
    with pytest.raises(scanner_git.ScanError) as caught:
        _ = scanner_git.run_git(
            path,
            "-c",
            "import sys;sys.stdout.write('x'*4096)",
        )
    assert caught.value.rule == scanner_git.GIT_OUTPUT_TOO_LARGE


@pytest.mark.parametrize("status", ["U", "X", "B", "Z"])
def test_ambiguous_tree_status_fails_closed(status: str) -> None:
    raw = status.encode() + b"\x00tests/file.py\x00"
    with pytest.raises(scanner_git.ScanError) as caught:
        _ = scanner.parse_entries(raw)
    assert caught.value.rule == scanner.MALFORMED_GIT_OUTPUT


def test_real_index_mutation_after_copy_does_not_change_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = repo(tmp_path)
    write_stage(path, "scripts/first.py", "print('first')\n")
    snapshot_id = _index_sha(path)
    original = scanner.run_git
    mutated = False
    copied_indexes: list[str] = []

    def mutate_real_index(
        cwd: Path,
        *args: str,
        environment: Mapping[str, str] | None = None,
        max_stdout_bytes: int | None = None,
    ) -> bytes:
        nonlocal mutated
        if environment is not None and not copied_indexes:
            copied_indexes.append(environment["GIT_INDEX_FILE"])
        if environment is not None and not mutated:
            mutated = True
            write_stage(path, "scripts/second.py", "print('second')\n")
        if environment is None:
            return original(cwd, *args)
        return original(
            cwd,
            *args,
            environment=environment,
            max_stdout_bytes=max_stdout_bytes,
        )

    monkeypatch.setattr(scanner, "run_git", mutate_real_index)
    result = scanner.scan(path)
    assert result.staged_files == 1
    assert result.snapshot_id == snapshot_id
    assert "scripts/second.py" in git(path, "diff", "--cached", "--name-only").stdout
    assert len(copied_indexes) == 1
    assert not Path(copied_indexes[0]).parent.exists()
