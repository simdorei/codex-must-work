from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from tests import staged_secret_scan_patterns as scanner_patterns
from tests.test_staged_secret_scan import git, git_bytes, joined, repo, run, write_stage

if TYPE_CHECKING:
    from pathlib import Path


def test_intent_to_add_index_entry_fails_closed(tmp_path: Path) -> None:
    path = repo(tmp_path)
    target = path / "scripts" / "pending.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_text("print('not staged')\n", encoding="utf-8")
    _ = git(path, "add", "-N", "scripts/pending.py")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert result.stdout.strip() == "INTENT_TO_ADD scripts/pending.py"


def test_token_shaped_filename_uses_hash_not_verbatim_path(tmp_path: Path) -> None:
    path = repo(tmp_path)
    relative = joined("tests/sk-", "live-12345678901234567890.bin")
    write_stage(path, relative, b"\x00binary")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert result.stdout.startswith("BINARY_BLOB path_sha256=")
    assert relative not in result.stdout


@pytest.mark.parametrize(
    "payload",
    [
        joined("AWS_", "SECRET_ACCESS_KEY=abcdefghijklmnopqrstuvwxyz0123456789ABCD+/==\n"),
        joined("stripe = 'sk_", "live_1234567890abcdefghijkl'\n"),
        joined("gitlab = 'glpat-", "123456789012345678901234567890'\n"),
        joined("google = 'AIza", "SyA1234567890abcdefghijklmnopqrstuv'\n"),
    ],
)
def test_canonical_credential_families_are_detected_without_echo(
    tmp_path: Path, payload: str
) -> None:
    path = repo(tmp_path)
    write_stage(path, "tests/credential_fixture.py", payload)
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert "tests/credential_fixture.py" in result.stdout
    assert payload.strip() not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "payload",
    [
        joined("stripe = 'sk_", "test_1234567890abcdefghijkl'\n"),
        joined("google = 'AIza", "-short-placeholder'\n"),
        joined("AWS_", "SECRET_ACCESS_KEY=placeholder\n"),
    ],
)
def test_credential_family_lookalikes_are_clean(tmp_path: Path, payload: str) -> None:
    path = repo(tmp_path)
    write_stage(path, "tests/credential-lookalike.py", payload)
    result = run(path, "--cached", "--redact")
    assert result.returncode == 0
    assert "findings=0" in result.stdout


def test_duplicate_unmerged_index_stages_fail_closed(tmp_path: Path) -> None:
    path = repo(tmp_path)
    oid = git(path, "rev-parse", ":README.md").stdout.strip()
    records = (
        f"100644 {oid} 1\tscripts/conflict.py\n100644 {oid} 2\tscripts/conflict.py\n"
    ).encode()
    _ = git_bytes(path, "update-index", "--index-info", input_data=records)
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert result.stdout.strip() == "UNMERGED_INDEX scripts/conflict.py"


@pytest.mark.parametrize(
    ("mode", "oid_source", "relative", "expected_rule"),
    [
        ("120000", ":README.md", "scripts/link", "SYMLINK_ENTRY"),
        ("160000", "HEAD", "runtime/submodule", "SUBMODULE_ENTRY"),
    ],
)
def test_non_regular_tree_entries_fail_closed(
    tmp_path: Path, mode: str, oid_source: str, relative: str, expected_rule: str
) -> None:
    path = repo(tmp_path)
    oid = git(path, "rev-parse", oid_source).stdout.strip()
    _ = git(path, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{relative}")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert result.stdout.strip() == f"{expected_rule} {relative}"


def test_newline_index_path_uses_hash_identifier(tmp_path: Path) -> None:
    path = repo(tmp_path)
    oid = git(path, "rev-parse", ":README.md").stdout.strip()
    relative = "tests/line\nfeed.bin"
    child_record = f"100644 blob {oid}\tline\nfeed.bin\0".encode()
    child_tree = git_bytes(path, "mktree", "-z", input_data=child_record).stdout.decode().strip()
    root_record = (f"100644 blob {oid}\tREADME.md\0040000 tree {child_tree}\ttests\0").encode()
    root_tree = git_bytes(path, "mktree", "-z", input_data=root_record).stdout.decode().strip()
    try:
        _ = git(path, "read-tree", root_tree)
    except subprocess.CalledProcessError:
        pytest.skip("Git for Windows rejects newline paths before they reach the index")
    result = run(path, "--cached", "--redact")
    assert result.returncode == 1
    assert result.stdout.startswith("UNSAFE_PATH path_sha256=")
    assert relative not in result.stdout


def test_control_path_display_is_hashed_without_control_bytes(tmp_path: Path) -> None:
    _ = repo(tmp_path)
    relative = "tests/line\nfeed.bin"
    displayed = scanner_patterns.display_path(relative)
    assert displayed.startswith("path_sha256=")
    assert relative not in displayed
