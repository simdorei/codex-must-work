from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TypedDict, cast

import pytest

from scripts.cache_semver import higher

ROOT = Path(__file__).resolve().parents[1]
OLD_VERSION = "0.1.0+codex.20260720221156"
EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
}
RELEASES = [
    (
        "0.144.0-alpha.4",
        "049586f41571e74b44c841868bca3a2233214a71",
        "6475d3787600d813f809098281252b2c361a6dae",
        36571,
        "650-675",
    ),
    (
        "0.144.0",
        "767822446c7a594caa19609ca435281a9ec67e0d",
        "6475d3787600d813f809098281252b2c361a6dae",
        36571,
        "650-675",
    ),
    (
        "0.145.0-alpha.18",
        "f84f9a6406cc55b210395f71b4c6aed236fc7ebb",
        "b3ef42c8e9201271e0bcbaa818b3bb1fc3963e3a",
        36804,
        "651-676",
    ),
]
EXCERPT_HASHES = [
    "e819e6a5a594a170a2e17538a84c113af5f9c1a2bb4f962339c83d7750383aa9",
    "e819e6a5a594a170a2e17538a84c113af5f9c1a2bb4f962339c83d7750383aa9",
    "0be0bcf6cca75e73e19856ad9c84141b7b75a447fc8a3f74a2cf2295118c0a6f",
]


class _ReleaseFixture(TypedDict):
    version: str
    commit: str
    blob_id: str
    blob_size: int
    excerpt_lines: str
    excerpt: str


class _RootParserFixture(TypedDict):
    source_path: str
    releases: list[_ReleaseFixture]


def _json(relative: str) -> dict[str, object]:
    value = cast("object", json.loads((ROOT / relative).read_text(encoding="utf-8")))
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _release_identities(fixture: _RootParserFixture) -> list[tuple[str, str, str, int, str]]:
    assert len(fixture["releases"]) == len(RELEASES)
    return [
        (
            release["version"],
            release["commit"],
            release["blob_id"],
            release["blob_size"],
            release["excerpt_lines"],
        )
        for release in fixture["releases"]
    ]


def _root_parser_fixture() -> _RootParserFixture:
    value = cast("object", _json("tests/fixtures/codex-marketplace-root-parser.json"))
    return cast("_RootParserFixture", value)


def test_metadata_marketplace_is_the_exact_local_contract() -> None:
    assert _json(".agents/plugins/marketplace.json") == {
        "name": "codex-must-work-local",
        "interface": {"displayName": "Codex Must Work Local"},
        "plugins": [
            {
                "name": "codex-must-work",
                "source": {"source": "local", "path": "./"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Developer Tools",
            }
        ],
    }


def test_version_and_manifest_hook_path_define_one_cache_identity() -> None:
    manifest = _json(".codex-plugin/plugin.json")
    version = manifest["version"]
    assert isinstance(version, str)
    assert version != "local"
    assert higher(version, OLD_VERSION)
    assert manifest["hooks"] == ["./hooks/hooks.json"]
    expected_cache = f"<CODEX_HOME>/plugins/cache/codex-must-work-local/codex-must-work/{version}"
    assert expected_cache in (ROOT / "README.md").read_text(encoding="utf-8")


def test_metadata_hook_manifest_has_exactly_three_lifecycle_events_and_paths() -> None:
    manifest = _json("hooks/hooks.json")
    hooks = cast("dict[str, list[dict[str, list[dict[str, object]]]]]", manifest["hooks"])
    assert set(hooks) == EVENTS
    assert all(len(groups) == 1 for groups in hooks.values())
    assert all(len(groups[0]["hooks"]) == 1 for groups in hooks.values())


def test_marketplace_root_fixture_pins_exact_release_blobs_and_excerpts() -> None:
    fixture = _root_parser_fixture()
    assert fixture["source_path"] == "codex-rs/core-plugins/src/marketplace.rs"
    releases = fixture["releases"]
    assert _release_identities(fixture) == RELEASES
    assert [hashlib.sha256(release["excerpt"].encode()).hexdigest() for release in releases] == (
        EXCERPT_HASHES
    )
    for release in releases:
        excerpt = release["excerpt"]
        assert '"." | "./" => return marketplace_root_dir(marketplace_path),' in excerpt
        assert '"" => {' in excerpt
        assert "Component::Normal" in excerpt or "let Some(relative_path)" in excerpt


@pytest.mark.parametrize("path", ["", "../plugin", "./../plugin", "plugin"])
def test_marketplace_root_rejects_empty_or_escaping_paths(path: str) -> None:
    accepted = path in {".", "./"} or (
        path.startswith("./") and all(part not in {"", ".", ".."} for part in path[2:].split("/"))
    )
    assert accepted is False


@pytest.mark.parametrize("path", [".", "./"])
def test_marketplace_root_accepts_repository_root(path: str) -> None:
    assert path in {".", "./"}


def test_metadata_contract_rejects_invalid_fixture() -> None:
    fixture = _root_parser_fixture()
    fixture["releases"] = []
    with pytest.raises(AssertionError):
        _ = _release_identities(fixture)


def test_readme_uses_only_root_trust_aware_installers() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert ".\\install.ps1" in readme
    assert "./install.sh" in readme
    assert "codex plugin marketplace add" not in readme
    assert "codex plugin add" not in readme
    assert "/hooks" in readme
    assert "필요하지 않습니다" in readme
    assert "재시작" in readme
    assert "새 스레드" in readme


def test_readme_documents_versions_diagnostics_and_owned_paths() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for version, *_ in RELEASES:
        assert version in readme
    for diagnostic in (
        "unsupported_codex_hook_contract: CMW must be updated for this Codex version",
        "unsupported_codex_marketplace_root",
        "codex_hooks_disabled",
        "codex_plugins_disabled",
        "managed_hooks_only",
        "managed_hook_policy_unverifiable",
        "cache_selection_conflict",
        "cache_same_version_mismatch",
        "codex_config_metadata_unsupported",
    ):
        assert diagnostic in readme
    for path in (
        "codex-must-work@codex-must-work-local",
        "<CODEX_HOME>/plugins/data/codex-must-work-codex-must-work-local",
        "[marketplaces.codex-must-work-local]",
        "<CODEX_HOME>/config.toml",
    ):
        assert path in readme


def test_readme_documents_update_migration_and_metadata_limits() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "소스 체크아웃을 이동하거나 삭제하지 마세요",
        "업데이트한 뒤 같은 설치 명령을 다시 실행",
        "codex-must-work@simdorei",
        "기존 캐시, 훅 상태, 작업 데이터와 보정 기록은 삭제하지 않습니다",
        "첫 스레드",
        "자동 적용하지 않습니다",
        "관리자 권한을 요청하지 않습니다",
        "audit SACL",
        "기존 `[notice]` 표는 바이트 단위로 변경하지 않습니다",
    )
    assert all(text in readme for text in required)
    assert re.search(r"예약 버전 `local`.*더 높은 버전", readme, re.DOTALL)
