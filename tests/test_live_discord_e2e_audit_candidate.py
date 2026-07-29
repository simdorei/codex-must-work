from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests import live_discord_e2e_audit as audit_module
from tests.live_discord_e2e_audit_candidate import (
    CandidateBindingError,
    load_candidate_binding,
    require_candidate_matches,
)
from tests.live_discord_e2e_audit_preflight import PreflightLocator, PreflightSnapshot
from tests.live_discord_e2e_audit_records import decode_json
from tests.test_live_discord_e2e_audit import CODEX_THREAD, SESSION, THREAD
from tests.test_live_discord_e2e_audit_preflight import (
    PACKAGE_DIGEST,
    SHA,
    preflight_snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path


def _receipt(path: Path, *, git_sha: str = SHA, digest: str = PACKAGE_DIGEST) -> None:
    _ = path.write_text(
        json.dumps(
            {
                "schema": "cmw_candidate_binding_v1",
                "git_sha": git_sha,
                "package_digest_sha256": digest,
            }
        ),
        encoding="utf-8",
    )


def _strict_preflight_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_binding: Path | None,
) -> list[str]:
    rollout = tmp_path / "rollout.jsonl"
    _ = rollout.write_text("", encoding="utf-8")
    locator = PreflightLocator(
        SESSION,
        rollout,
        tmp_path / "installed-plugin",
        tmp_path / "data",
        "dontAsk",
        PACKAGE_DIGEST,
    )

    def located(_path: Path) -> PreflightLocator:
        return locator

    def mapped(_database: Path, _thread: str) -> str:
        return CODEX_THREAD

    monkeypatch.setattr(audit_module, "load_preflight_locator", located)
    monkeypatch.setattr(audit_module, "load_mapping", mapped)

    def collect(
        _locator: PreflightLocator,
        _discord_thread_id: str,
        _codex_thread_id: str,
        _expected_digest: str | None,
    ) -> PreflightSnapshot:
        return preflight_snapshot()

    monkeypatch.setattr(audit_module, "collect_preflight", collect)
    argv = [
        "--rollout",
        str(rollout),
        "--discord-log",
        "handoffs/todo10-discord-audit/redacted-native-discord-messages.jsonl",
        "--thread-id",
        THREAD,
        "--expected-sha",
        SHA,
        "--preflight-only",
        "--require-candidate-binding",
    ]
    if candidate_binding is not None:
        argv.extend(("--candidate-binding", str(candidate_binding)))
    return argv


def test_external_candidate_receipt_binds_full_git_sha_to_actual_package(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "installed-plugin"
    plugin_root.mkdir()
    receipt_path = tmp_path / "todo12-candidate-binding.json"
    _receipt(receipt_path)

    binding = load_candidate_binding(receipt_path, plugin_root)
    require_candidate_matches(
        binding,
        actual_package_digest_sha256=PACKAGE_DIGEST,
        expected_git_sha=SHA,
        expected_package_digest_sha256=PACKAGE_DIGEST,
    )

    assert binding.git_sha == SHA
    assert binding.package_digest_sha256 == PACKAGE_DIGEST


@pytest.mark.parametrize(
    ("actual_digest", "expected_sha", "expected_digest", "reason"),
    [
        ("c" * 64, SHA, PACKAGE_DIGEST, "candidate_binding_package_mismatch"),
        (PACKAGE_DIGEST, "c" * 40, PACKAGE_DIGEST, "candidate_binding_git_mismatch"),
        (PACKAGE_DIGEST, SHA, "c" * 64, "candidate_binding_package_mismatch"),
    ],
)
def test_candidate_receipt_rejects_every_binding_mismatch(
    tmp_path: Path,
    actual_digest: str,
    expected_sha: str,
    expected_digest: str,
    reason: str,
) -> None:
    plugin_root = tmp_path / "installed-plugin"
    plugin_root.mkdir()
    receipt_path = tmp_path / "todo12-candidate-binding.json"
    _receipt(receipt_path)
    binding = load_candidate_binding(receipt_path, plugin_root)

    with pytest.raises(CandidateBindingError, match=reason):
        require_candidate_matches(
            binding,
            actual_package_digest_sha256=actual_digest,
            expected_git_sha=expected_sha,
            expected_package_digest_sha256=expected_digest,
        )


def test_candidate_receipt_cannot_be_self_embedded_in_installed_package(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "installed-plugin"
    plugin_root.mkdir()
    receipt_path = plugin_root / "candidate-binding.json"
    _receipt(receipt_path)

    with pytest.raises(CandidateBindingError, match="candidate_binding_self_embedded"):
        _ = load_candidate_binding(receipt_path, plugin_root)


def test_todo15_strict_preflight_requires_and_verifies_external_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt_path = tmp_path / "todo12-candidate-binding.json"
    _receipt(receipt_path)
    argv = _strict_preflight_args(tmp_path, monkeypatch, receipt_path)

    exit_code = audit_module.main(argv)
    values = decode_json(capsys.readouterr().out)
    assert isinstance(values, dict)

    assert exit_code == 0
    assert values["candidate_binding_matches"] is True
    assert values["discord_bot_author_id"] == "discord-bot"
    assert "source_git_sha" not in values


def test_todo15_strict_preflight_rejects_missing_candidate_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _strict_preflight_args(tmp_path, monkeypatch, None)

    exit_code = audit_module.main(argv)

    assert exit_code == 1
    assert "candidate_binding_required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("git_sha", "digest"),
    [("short", PACKAGE_DIGEST), (SHA, "short")],
)
def test_candidate_receipt_requires_full_typed_hashes(
    tmp_path: Path,
    git_sha: str,
    digest: str,
) -> None:
    plugin_root = tmp_path / "installed-plugin"
    plugin_root.mkdir()
    receipt_path = tmp_path / "candidate-binding.json"
    _receipt(receipt_path, git_sha=git_sha, digest=digest)

    with pytest.raises(CandidateBindingError, match="candidate_binding_invalid"):
        _ = load_candidate_binding(receipt_path, plugin_root)
