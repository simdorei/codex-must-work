from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from scripts.install_errors import InstallPluginError
from scripts.installer_mcp_runtime import RuntimeSpec, prepare_mcp_runtime
from tests.test_installer_mcp_runtime import archive_fixture


def _file(name: str, data: bytes = b"bytecode") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o600
    return member, data


def _directory(name: str) -> tuple[tarfile.TarInfo, None]:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    return member, None


def _symlink(name: str) -> tuple[tarfile.TarInfo, None]:
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = "python/python.exe"
    return member, None


def _rewrite_archive(
    source: Path,
    spec: RuntimeSpec,
    bytecode: tuple[tuple[str, bytes], ...],
) -> RuntimeSpec:
    archive = source / "runtime" / "archives" / spec.archive_name
    with tarfile.open(archive, "w:gz") as bundle:
        executable = tarfile.TarInfo("python/python.exe")
        executable.size = len(b"runtime")
        executable.mode = 0o755
        bundle.addfile(executable, io.BytesIO(b"runtime"))
        for name, contents in bytecode:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = 0o600
            bundle.addfile(member, io.BytesIO(contents))
    return replace(spec, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())


@pytest.mark.parametrize(
    "member",
    [
        _file("../evil.pyc"),
        _file("/absolute/evil.pyc"),
        _file("C:/drive/evil.pyc"),
    ],
)
def test_prepare_rejects_unsafe_bytecode_path(
    tmp_path: Path,
    member: tuple[tarfile.TarInfo, bytes],
) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(
        source,
        executable="python/python.exe",
        extra_members=(member,),
    )

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


def test_prepare_rejects_duplicate_bytecode_member(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    duplicate = _file("python/Lib/__pycache__/evil.cpython-312.pyc")
    spec = archive_fixture(
        source,
        executable="python/python.exe",
        extra_members=(duplicate, duplicate),
    )

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


def test_prepare_rejects_normalized_bytecode_path_collision(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    expected = "python/Lib/__pycache__/expected.cpython-312.pyc"
    spec = archive_fixture(
        source,
        executable="python/python.exe",
        excluded_bytecode=((expected, b"expected"),),
    )
    rewritten = _rewrite_archive(
        source,
        spec,
        (
            (expected, b"expected"),
            ("python/Lib/./__pycache__/expected.cpython-312.pyc", b"expected"),
        ),
    )

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, rewritten)


@pytest.mark.parametrize(
    "member",
    [
        _directory("python/Lib/evil.pyc"),
        _symlink("python/Lib/evil.pyc"),
    ],
)
def test_prepare_rejects_non_file_bytecode_member(
    tmp_path: Path,
    member: tuple[tarfile.TarInfo, None],
) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(
        source,
        executable="python/python.exe",
        extra_members=(member,),
    )

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


def test_prepare_rejects_unlisted_valid_bytecode_member(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(
        source,
        executable="python/python.exe",
        extra_members=(_file("python/Lib/__pycache__/evil.cpython-312.pyc"),),
    )

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


@pytest.mark.parametrize(
    "actual",
    [
        (),
        (("python/Lib/__pycache__/expected.cpython-312.pyc", b"changed"),),
    ],
)
def test_prepare_rejects_missing_or_changed_expected_bytecode(
    tmp_path: Path,
    actual: tuple[tuple[str, bytes], ...],
) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    expected = "python/Lib/__pycache__/expected.cpython-312.pyc"
    spec = archive_fixture(
        source,
        executable="python/python.exe",
        excluded_bytecode=((expected, b"expected"),),
    )
    rewritten = _rewrite_archive(source, spec, actual)

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, rewritten)


def test_committed_bytecode_exclusions_have_pinned_counts_and_hashes() -> None:
    # Given
    root = Path(__file__).resolve().parents[1]
    metadata = cast(
        "dict[str, object]",
        json.loads((root / "runtime" / "manifest.json").read_text(encoding="utf-8")),
    )
    targets = cast("dict[str, dict[str, int | str]]", metadata["targets"])

    # When / Then
    expected_counts = {"windows-x64": 554, "linux-x64": 3, "macos-arm64": 3}
    for target, expected_count in expected_counts.items():
        target_metadata = targets[target]
        exclusion_name = target_metadata["bytecode_exclusion"]
        exclusion_hash = target_metadata["bytecode_exclusion_sha256"]
        assert isinstance(exclusion_name, str)
        assert isinstance(exclusion_hash, str)
        path = root / "runtime" / "exclusions" / exclusion_name
        payload = path.read_bytes()
        entries = cast("list[dict[str, bool | int | str]]", json.loads(payload))
        assert target_metadata["bytecode_exclusion_count"] == expected_count
        assert len(entries) == expected_count
        assert hashlib.sha256(payload).hexdigest() == exclusion_hash
        for entry in entries:
            entry_path = entry["path"]
            assert entry["type"] == "file"
            assert isinstance(entry_path, str)
            assert entry_path.endswith(".pyc")
