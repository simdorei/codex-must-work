from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.install_errors import InstallPluginError
from scripts.installer_mcp_runtime import (
    RuntimePlatform,
    RuntimeSpec,
    prepare_mcp_runtime,
    remove_created_mcp_runtime,
)


def archive_fixture(
    source_root: Path,
    *,
    executable: str,
    contents: bytes = b"runtime",
    extra_members: tuple[tuple[tarfile.TarInfo, bytes | None], ...] = (),
    excluded_bytecode: tuple[tuple[str, bytes], ...] = (),
) -> RuntimeSpec:
    archive = source_root / "runtime" / "archives" / "test-runtime.tar.gz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo(executable)
        entry.size = len(contents)
        entry.mode = 0o755
        bundle.addfile(entry, io.BytesIO(contents))
        for extra, data in extra_members:
            bundle.addfile(extra, None if data is None else io.BytesIO(data))
        for name, data in excluded_bytecode:
            excluded = tarfile.TarInfo(name)
            excluded.size = len(data)
            bundle.addfile(excluded, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    platform = RuntimePlatform.WINDOWS if executable.endswith(".exe") else RuntimePlatform.POSIX
    relative = executable.removeprefix("python/")
    files = {
        relative: {
            "executable": True,
            "path": relative,
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size": len(contents),
            "type": "file",
        }
    }
    if platform is RuntimePlatform.POSIX:
        wrapper = (
            b"#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\n"
            b'exec "$(dirname -- "$0")/bin/python3" -B "$@"\n'
        )
        files["python"] = {
            "executable": True,
            "path": "python",
            "sha256": hashlib.sha256(wrapper).hexdigest(),
            "size": len(wrapper),
            "type": "file",
        }
    directories = {
        "/".join(parts[:index])
        for path in files
        for parts in (path.split("/"),)
        for index in range(1, len(parts))
    }
    empty_digest = hashlib.sha256(b"").hexdigest()
    entries = [
        {
            "executable": False,
            "path": path,
            "sha256": empty_digest,
            "size": 0,
            "type": "directory",
        }
        for path in directories
    ]
    entries.extend(files.values())
    entries.sort(key=lambda item: str(item["path"]).encode())
    manifest = source_root / "runtime" / "manifests" / "test.json"
    manifest.parent.mkdir(parents=True)
    _ = manifest.write_text(
        json.dumps(entries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    exclusion_entries = [
        {
            "executable": False,
            "path": name.removeprefix("python/"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "type": "file",
        }
        for name, data in excluded_bytecode
    ]
    exclusion_entries.sort(key=lambda item: str(item["path"]).encode())
    exclusion = source_root / "runtime" / "exclusions" / "test.json"
    exclusion.parent.mkdir(parents=True)
    _ = exclusion.write_text(
        json.dumps(exclusion_entries, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    exclusion_digest = hashlib.sha256(exclusion.read_bytes()).hexdigest()
    return RuntimeSpec(
        "test",
        archive.name,
        digest,
        platform,
        manifest.name,
        manifest_digest,
        exclusion.name,
        exclusion_digest,
        len(exclusion_entries),
    )


def test_runtime_is_extracted_once_and_reused_by_identity(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")

    # When
    created = prepare_mcp_runtime(source, data, spec)
    reused = prepare_mcp_runtime(source, data, spec)

    # Then
    assert created.created_by_run is True
    assert reused.created_by_run is False
    assert reused.identity == created.identity
    assert created.path == data / "portable-python-test"
    assert (created.path / "python.exe").read_bytes() == b"runtime"


def test_posix_runtime_gets_one_exec_wrapper_at_common_path(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/bin/python3")

    # When
    publication = prepare_mcp_runtime(source, data, spec)

    # Then
    wrapper = publication.path / "python"
    assert wrapper.read_text(encoding="utf-8") == (
        "#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\n"
        'exec "$(dirname -- "$0")/bin/python3" -B "$@"\n'
    )
    if os.name != "nt":
        assert wrapper.stat().st_mode & 0o111 == 0o111


def test_archive_hash_mismatch_fails_without_publishing_runtime(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")
    mismatched = RuntimeSpec(
        spec.version,
        spec.archive_name,
        "0" * 64,
        spec.platform,
        spec.manifest_name,
        spec.manifest_sha256,
        spec.exclusion_name,
        spec.exclusion_sha256,
        spec.exclusion_count,
    )

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_archive_hash_mismatch"):
        _ = prepare_mcp_runtime(source, data, mismatched)
    assert not (data / "portable-python-test").exists()


def test_failed_transaction_removes_only_runtime_it_created(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    sentinel = data / "keep.txt"
    _ = sentinel.write_text("keep", encoding="utf-8")
    spec = archive_fixture(source, executable="python/python.exe")
    publication = prepare_mcp_runtime(source, data, spec)

    # When
    remove_created_mcp_runtime(data, publication)

    # Then
    assert not publication.path.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_reused_runtime_is_never_removed_by_later_transaction(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")
    _ = prepare_mcp_runtime(source, data, spec)
    reused = prepare_mcp_runtime(source, data, spec)

    # When
    remove_created_mcp_runtime(data, reused)

    # Then
    assert reused.path.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows extensionless spawn contract")
def test_windows_versioned_runtime_starts_through_extensionless_mcp_path(tmp_path: Path) -> None:
    # Given
    source = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"
    data.mkdir()
    publication = prepare_mcp_runtime(source, data)
    executable = str(publication.path / "python")

    # When
    completed = subprocess.run(  # noqa: S603
        [
            executable,
            "-I",
            "-c",
            "__import__('sys').exit(0)",
        ],
        check=False,
        capture_output=True,
    )

    # Then
    assert completed.returncode == 0
