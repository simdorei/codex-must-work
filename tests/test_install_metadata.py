from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol, TypedDict

import pytest

from scripts.cache_semver import higher
from scripts.installer_cache_observation import selected_cache_root
from scripts.marketplace_identity import (
    DATA_ROOT_NAME,
    MARKETPLACE_NAME,
    PLUGIN_ID,
    PLUGIN_NAME,
)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class _JsonLoader(Protocol):
    def __call__(self, s: str) -> JsonValue: ...


def _load_json(loader: _JsonLoader, data: str) -> JsonValue:
    return loader(data)


ROOT = Path(__file__).resolve().parents[1]
OLD_VERSION = "0.1.0+codex.20260720221156"
EVENTS = {"UserPromptSubmit"}
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


def _json(relative: str) -> JsonObject:
    value = _load_json(json.loads, (ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        pytest.fail(f"expected JSON object: {relative}")
    return value


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
    value = _json("tests/fixtures/codex-marketplace-root-parser.json")
    source_path = value.get("source_path")
    releases = value.get("releases")
    if not isinstance(source_path, str) or not isinstance(releases, list):
        pytest.fail("root parser fixture shape is invalid")
    parsed: list[_ReleaseFixture] = []
    for release in releases:
        if not isinstance(release, dict):
            pytest.fail("root parser release shape is invalid")
        version = release.get("version")
        commit = release.get("commit")
        blob_id = release.get("blob_id")
        blob_size = release.get("blob_size")
        excerpt_lines = release.get("excerpt_lines")
        excerpt = release.get("excerpt")
        if (
            not isinstance(version, str)
            or not isinstance(commit, str)
            or not isinstance(blob_id, str)
            or not isinstance(blob_size, int)
            or not isinstance(excerpt_lines, str)
            or not isinstance(excerpt, str)
        ):
            pytest.fail("root parser release fields are invalid")
        parsed.append(
            _ReleaseFixture(
                version=version,
                commit=commit,
                blob_id=blob_id,
                blob_size=blob_size,
                excerpt_lines=excerpt_lines,
                excerpt=excerpt,
            )
        )
    return _RootParserFixture(source_path=source_path, releases=parsed)


def test_metadata_marketplace_is_the_exact_public_contract() -> None:
    assert _json(".agents/plugins/marketplace.json") == {
        "name": "simdorei",
        "interface": {"displayName": "simdorei"},
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


def test_version_and_default_hook_path_define_one_cache_identity(
    tmp_path: Path,
) -> None:
    manifest = _json(".codex-plugin/plugin.json")
    version = manifest["version"]
    assert isinstance(version, str)
    assert version != "local"
    assert higher(version, OLD_VERSION)
    assert "hooks" not in manifest
    assert (ROOT / "hooks" / "hooks.json").is_file()
    cache = tmp_path / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / version
    metadata = cache / ".codex-plugin"
    metadata.mkdir(parents=True)
    _ = (metadata / "plugin.json").write_text(
        json.dumps({"name": PLUGIN_NAME, "version": version}),
        encoding="utf-8",
    )

    assert selected_cache_root(tmp_path) == cache
    assert f"{PLUGIN_NAME}@{MARKETPLACE_NAME}" == PLUGIN_ID
    assert f"{PLUGIN_NAME}-{MARKETPLACE_NAME}" == DATA_ROOT_NAME


def test_metadata_hook_manifest_has_only_explicit_prompt_and_exact_path() -> None:
    manifest = _json("hooks/hooks.json")
    hooks = manifest.get("hooks")
    assert isinstance(hooks, dict)
    assert set(hooks) == EVENTS
    for groups in hooks.values():
        assert isinstance(groups, list)
        assert len(groups) == 1
        group = groups[0]
        assert isinstance(group, dict)
        handlers = group.get("hooks")
        assert isinstance(handlers, list)
        assert len(handlers) == 1


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
