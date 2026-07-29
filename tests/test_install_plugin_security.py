from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts import install_plugin
from scripts.install_errors import InstallPluginError
from scripts.install_plugin import install
from scripts.package_secret_scan import scan_package_candidate
from scripts.package_snapshot import package_candidate_snapshot
from tests.install_plugin_support import failure_case, publisher

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ("tests.install_plugin_fixtures",)


def test_secret_like_package_aborts_before_target_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    synthetic_install_receipt: None,
) -> None:
    # Given
    _ = synthetic_install_receipt
    home, source, _ = failure_case(tmp_path, monkeypatch)
    secret_path = source / "candidate.env"
    candidate_content = "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/" + "A" * 68
    _ = secret_path.write_text(candidate_content, encoding="utf-8")
    package_files = [
        ".codex-plugin/plugin.json",
        "candidate.env",
        "hooks/hooks.json",
        "runtime/package-files.json",
    ]
    _ = (source / "runtime" / "package-files.json").write_text(
        json.dumps(package_files),
        encoding="utf-8",
    )
    before = tuple(sorted(path.relative_to(home) for path in home.rglob("*")))

    # When
    result = install(home.resolve(), source)

    # Then
    captured = capsys.readouterr()
    assert result.error_code == "package_candidate_secret_detected"
    assert candidate_content not in f"{result!r}{captured.out}{captured.err}"
    assert tuple(sorted(path.relative_to(home) for path in home.rglob("*"))) == before


def test_safe_package_candidate_proceeds_to_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_install_receipt: None,
) -> None:
    # Given
    _ = synthetic_install_receipt
    home, source, _ = failure_case(tmp_path, monkeypatch)
    monkeypatch.setattr(install_plugin, "publish_cache", publisher(home))

    # When
    result = install(home.resolve(), source)

    # Then
    assert result.install_ok


def test_source_swap_after_final_scan_never_reaches_cache_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_install_receipt: None,
) -> None:
    # Given: a safe candidate whose original source is swapped after the final scan returns.
    _ = synthetic_install_receipt
    home, source, _ = failure_case(tmp_path, monkeypatch)
    candidate = source / "candidate.env"
    _ = candidate.write_text("SAFE=1", encoding="utf-8")
    manifest = [
        ".codex-plugin/plugin.json",
        "candidate.env",
        "hooks/hooks.json",
        "runtime/package-files.json",
    ]
    _ = (source / "runtime" / "package-files.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    scan_count = 0
    published_bytes: list[bytes] = []
    secret = ("https://discord.com/api/webhooks/123456789/" + "A" * 68).encode()

    def scan_then_swap(candidate_root: Path) -> str:
        nonlocal scan_count
        seal = scan_package_candidate(candidate_root)
        scan_count += 1
        if scan_count == 2:
            _ = candidate.write_bytes(secret)
        return seal

    def capture_publication(candidate_root: Path, _home: Path, _version: str) -> None:
        published_bytes.append((candidate_root / "candidate.env").read_bytes())
        reason = "stop_after_publication_capture"
        raise InstallPluginError(reason)

    monkeypatch.setattr(install_plugin, "scan_package_candidate", scan_then_swap)
    monkeypatch.setattr(install_plugin, "publish_cache", capture_publication)

    # When: installation reaches the publication seam.
    result = install(home.resolve(), source)

    # Then: only the scanned snapshot bytes are offered to cache publication.
    assert result.error_code == "stop_after_publication_capture"
    assert published_bytes == [b"SAFE=1"]
    assert secret not in published_bytes


def test_candidate_snapshot_is_removed_after_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_install_receipt: None,
) -> None:
    # Given: one valid source and a captured isolated candidate path.
    _ = synthetic_install_receipt
    _, source, _ = failure_case(tmp_path, monkeypatch)
    candidate_roots: list[Path] = []

    # When: a forced interruption crosses the snapshot context boundary.
    def interrupt() -> None:
        raise KeyboardInterrupt

    def interrupt_snapshot() -> None:
        with package_candidate_snapshot(source) as snapshot:
            candidate_roots.append(snapshot)
            interrupt()

    with pytest.raises(KeyboardInterrupt):
        interrupt_snapshot()

    # Then: BaseException cleanup removed the entire isolated candidate root.
    assert len(candidate_roots) == 1
    assert not candidate_roots[0].exists()
