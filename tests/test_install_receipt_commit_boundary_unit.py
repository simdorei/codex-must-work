from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts import install_receipt
from scripts.cache_types import CacheIdentity, CachePublication
from scripts.install_errors import InstallPluginError
from scripts.install_plugin_cli import run_cli
from scripts.install_receipt import InstallReceipt, ReceiptCommit
from scripts.installer_lock import installer_lock
from scripts.installer_mcp_runtime import McpRuntimePublication
from scripts.installer_result import InstallResult, install_success

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from scripts.installer_lock import InstallerLease


_WARNING = "install_completion_cleanup_pending"


def _installation(tmp_path: Path) -> tuple[CachePublication, McpRuntimePublication, InstallReceipt]:
    cache_identity = CacheIdentity(1, 2)
    runtime_identity = CacheIdentity(3, 4)
    cache = tmp_path / "cache"
    runtime_path = tmp_path / "runtime"
    publication = CachePublication(
        cache_path=cache,
        digest="a" * 64,
        created_by_run=True,
        identity=cache_identity,
    )
    runtime = McpRuntimePublication(
        path=runtime_path,
        identity=runtime_identity,
        created_by_run=True,
    )
    receipt = InstallReceipt(
        cache,
        "0.2.0",
        cache_identity,
        publication.digest,
        tmp_path,
        (),
        runtime_path,
        runtime_identity,
        "runtime-version",
    )
    return publication, runtime, receipt


def test_exact_receipt_is_the_commit_classifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication, runtime, receipt = _installation(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    def load_receipt(_lease: InstallerLease, _source: Path) -> InstallReceipt:
        return receipt

    monkeypatch.setattr(
        "scripts.install_receipt.load_install_receipt",
        load_receipt,
    )

    mismatched = publication._replace(digest="b" * 64)
    with installer_lock(home.resolve()) as lease:
        assert install_receipt.install_receipt_is_committed(
            lease,
            tmp_path,
            publication,
            runtime,
        )
        assert not install_receipt.install_receipt_is_committed(
            lease,
            tmp_path,
            mismatched,
            runtime,
        )


def test_cleanup_failure_returns_and_serializes_public_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    publication, runtime, receipt = _installation(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    def build_receipt(
        _lease: InstallerLease,
        _source: Path,
        _publication: CachePublication,
        _runtime: McpRuntimePublication,
    ) -> InstallReceipt:
        return receipt

    def load_receipt(_lease: InstallerLease, _source: Path) -> InstallReceipt:
        return receipt

    monkeypatch.setattr(
        "scripts.install_receipt._receipt_from_live",
        build_receipt,
    )
    monkeypatch.setattr(
        "scripts.install_receipt.load_install_receipt",
        load_receipt,
    )

    private_reason = "private_cleanup_detail"

    def fail_clear(_lease: InstallerLease) -> None:
        raise InstallPluginError(private_reason)

    def installer(codex_home: Path, source_root: Path) -> InstallResult:
        _ = codex_home, source_root
        return result

    monkeypatch.setattr("scripts.install_receipt.clear_uninstall_complete", fail_clear)
    with installer_lock(home.resolve()) as lease:
        commit = install_receipt.publish_install_receipt(lease, tmp_path, publication, runtime)
    result = install_success(commit.warning_code)
    exit_code = run_cli(installer, [str(tmp_path), str(tmp_path)])
    streams = capsys.readouterr()

    assert commit == ReceiptCommit(_WARNING)
    assert exit_code == 0
    assert streams.out == "install=ok\n"
    assert json.loads(streams.err) == {"warning_code": _WARNING}
    assert "private_cleanup_detail" not in streams.err
