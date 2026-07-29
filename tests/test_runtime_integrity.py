from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict, cast

import pytest

from scripts.install_errors import InstallPluginError
from scripts.installer_mcp_runtime import prepare_mcp_runtime, remove_created_mcp_runtime
from tests.test_installer_mcp_runtime import archive_fixture


class _RuntimeTarget(TypedDict):
    tree_manifest: str


class _RuntimeManifest(TypedDict):
    targets: dict[str, _RuntimeTarget]


class _ManifestEntry(TypedDict):
    executable: bool
    path: str
    sha256: str
    size: int
    type: str


class _McpServer(TypedDict):
    args: list[str]
    command: str
    cwd: str
    env: dict[str, str]


class _McpConfig(TypedDict):
    mcpServers: dict[str, _McpServer]


def test_reuse_rejects_tampered_runtime_bytes(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")
    publication = prepare_mcp_runtime(source, data, spec)
    _ = (publication.path / "python.exe").write_bytes(b"tampered")

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


def test_reuse_rejects_unexpected_runtime_member(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")
    publication = prepare_mcp_runtime(source, data, spec)
    _ = (publication.path / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode contract")
def test_reuse_rejects_executable_mode_change(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/bin/python3")
    publication = prepare_mcp_runtime(source, data, spec)
    executable = publication.path / "bin" / "python3"
    executable.chmod(0o600)

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="host has no symlink API")
def test_reuse_rejects_child_symlink(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")
    publication = prepare_mcp_runtime(source, data, spec)
    link = publication.path / "redirect"
    try:
        link.symlink_to(publication.path / "python.exe")
    except OSError as error:
        error_code = error.winerror if os.name == "nt" else error.errno
        pytest.skip(f"host cannot create test symlink: {error_code}")

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


@pytest.mark.skipif(not hasattr(os, "link"), reason="host has no hardlink API")
def test_reuse_rejects_child_hardlink(tmp_path: Path) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")
    publication = prepare_mcp_runtime(source, data, spec)
    os.link(publication.path / "python.exe", publication.path / "alias.exe")

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_invalid"):
        _ = prepare_mcp_runtime(source, data, spec)


@pytest.mark.skipif(
    os.name == "nt" or not os.supports_dir_fd,
    reason="POSIX dir_fd path-swap contract",
)
def test_rollback_path_swap_aborts_without_deleting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    source = tmp_path / "source"
    data = tmp_path / "data"
    data.mkdir()
    spec = archive_fixture(source, executable="python/python.exe")
    publication = prepare_mcp_runtime(source, data, spec)
    displaced = data / "displaced-runtime"
    victim = publication.path / "victim.txt"
    original_open = os.open
    swapped = False

    def swap_before_handle_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is not None and os.fsdecode(path) == publication.path.name:
            _ = publication.path.rename(displaced)
            _ = publication.path.mkdir()
            _ = victim.write_text("keep", encoding="utf-8")
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_handle_open)

    # When / Then
    with pytest.raises(InstallPluginError, match="portable_runtime_cleanup_conflict"):
        remove_created_mcp_runtime(data, publication)
    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "keep"


def test_committed_runtime_manifests_are_declared_and_well_formed() -> None:
    # Given
    root = Path(__file__).parents[1]
    runtime_manifest = cast(
        "_RuntimeManifest",
        json.loads((root / "runtime" / "manifest.json").read_text(encoding="utf-8")),
    )

    # When / Then
    for target in runtime_manifest["targets"].values():
        manifest_name = target["tree_manifest"]
        entries = cast(
            "list[_ManifestEntry]",
            json.loads(
                (root / "runtime" / "manifests" / manifest_name).read_text(encoding="utf-8")
            ),
        )
        assert entries
        expected_keys = {"executable", "path", "sha256", "size", "type"}
        assert all(set(entry) == expected_keys for entry in entries)
        assert all(entry["type"] in {"directory", "file"} for entry in entries)
        assert all(
            "\\" not in entry["path"] and not entry["path"].startswith("/") for entry in entries
        )


def test_mcp_launch_disables_bytecode_and_keeps_relative_layout() -> None:
    # Given
    root = Path(__file__).parents[1]
    config = cast(
        "_McpConfig",
        json.loads((root / ".mcp.json").read_text(encoding="utf-8")),
    )
    server = config["mcpServers"]["codex-must-work"]

    # When / Then
    assert server["command"] == "runtime/launch-python.exe"
    assert server["args"] == ["scripts/mcp_bootstrap.py"]
    assert server["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert server["cwd"] == "."


def test_real_mcp_import_and_reinstall_leave_runtime_and_source_bytecode_free(
    tmp_path: Path,
) -> None:
    # Given
    root = Path(__file__).parents[1]
    data = tmp_path / "data"
    data.mkdir()
    isolated = tmp_path / "isolated-plugin"
    _ = shutil.copytree(
        root / "scripts",
        isolated / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    publication = prepare_mcp_runtime(root, data)
    before = _tree_digest(publication.path)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    _ = environment.pop("PYTHONPATH", None)

    # When
    completed = subprocess.run(  # noqa: S603
        [
            str(publication.path / "python"),
            "-B",
            "-c",
            "import scripts.mcp_server",
        ],
        cwd=isolated,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    reused = prepare_mcp_runtime(root, data)

    # Then
    assert completed.returncode == 0, completed.stderr
    assert reused.created_by_run is False
    assert _tree_digest(reused.path) == before
    assert not tuple(isolated.rglob("*.pyc"))
    assert not tuple(publication.path.rglob("*.pyc"))


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().encode()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()
