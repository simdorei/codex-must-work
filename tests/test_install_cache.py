from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from scripts import install_cache
from scripts.install_cache import CachePublication, publish_cache
from scripts.install_errors import InstallPluginError
from scripts.state_io import open_direct_file

ROOT = Path(__file__).resolve().parents[1]
ORACLE = ROOT / "tests" / "fixtures" / "package-files-base-0352.txt"
ORACLE_SHA = "5eee478bec8407930ef8f38fa4e411fa9ff202771d00893485f1220bbb37ddb8"
GOLDEN = "d073c2db6f4ecafccba71b30ebacda30e99c61bb449e06b013f9c40dfdc6ab68"
MANIFEST = "runtime/package-files.json"


def _cases(value: str) -> tuple[str, ...]:
    return tuple(value.split())


PACKAGE_FAILURES = (
    *_cases("escape duplicate unsorted bad-json bad-hooks missing"),
    *_cases("bad-version escape-version slash-version"),
)
PATH_CASES = _cases("target-content target-extra target-missing source-symlink open-race path-swap")
type CacheCase = tuple[Path, Path, tuple[str, ...]]


def _source(root: Path) -> tuple[str, ...]:
    selected = {
        ".codex-plugin/plugin.json": b'{"name":"fixture"}\n',
        "hooks/hooks.json": b'{"hooks":{}}\n',
        "payload/a.txt": b"A",
    }
    paths = tuple(sorted((*selected, MANIFEST), key=str.encode))
    selected[MANIFEST] = json.dumps(paths, indent=2).encode() + b"\n"
    for relative, data in selected.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)
    return paths


def _cache(home: Path, version: str = "1.0.0") -> Path:
    return home / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / version


def _digest(root: Path, paths: tuple[str, ...]) -> str:
    value = hashlib.sha256(b"codex-must-work-package-v1\0" + struct.pack(">I", len(paths)))
    for relative in sorted(paths, key=str.encode):
        encoded = relative.encode()
        data = root.joinpath(*relative.split("/")).read_bytes()
        value.update(
            struct.pack(">I", len(encoded)) + encoded + struct.pack(">Q", len(data)) + data
        )
    return value.hexdigest()


def _snapshot(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _publish(case: CacheCase, version: str = "1.0.0") -> CachePublication:
    return publish_cache(case[0], case[1], version)


def _target(case: CacheCase, version: str = "1.0.0") -> Path:
    return _cache(case[1], version)


@pytest.fixture
def cache_case(tmp_path: Path) -> CacheCase:
    source, home = tmp_path / "source", tmp_path / "home"
    paths = _source(source)
    home.mkdir()
    return source.resolve(), home.resolve(), paths


def test_oracle_and_manifest_are_exact() -> None:
    oracle_data = ORACLE.read_bytes()
    oracle = oracle_data.decode().splitlines()
    manifest = sorted([*oracle, MANIFEST], key=str.encode)
    assert (len(oracle), hashlib.sha256(oracle_data).hexdigest()) == (145, ORACLE_SHA)
    assert oracle_data.endswith(b"\n")
    assert b"\r" not in oracle_data
    assert oracle == sorted(oracle, key=str.encode)
    assert len(manifest) == len(set(manifest)) == 146
    assert all((ROOT / path).is_file() for path in manifest)
    installer_only = {
        "scripts/codex_config.py",
        "scripts/config_metadata.py",
        "scripts/config_publication.py",
        "scripts/hook_trust.py",
        "scripts/install_cache.py",
        "scripts/install_errors.py",
        "scripts/installer_lock.py",
        "scripts/windows_file.py",
        *(f"scripts/{path.name}" for path in (ROOT / "scripts").glob("cache_*.py")),
    }
    assert installer_only.isdisjoint(manifest)
    assert not any(path.startswith("tests/") for path in manifest)
    assert (ROOT / MANIFEST).read_bytes() == json.dumps(manifest, indent=2).encode() + b"\n"


def test_independent_two_file_digest_has_fixed_golden(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    _ = (tmp_path / "a.txt").write_bytes(b"A")
    _ = (tmp_path / "nested" / "b.bin").write_bytes(bytes.fromhex("00 ff 0a"))
    assert _digest(tmp_path, ("a.txt", "nested/b.bin")) == GOLDEN


def test_publish_returns_independent_durable_private_tree(
    cache_case: CacheCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    flushed: set[str] = set()

    def flush(path: Path) -> None:
        if ".cmw-install-staging" in path.parts:
            offset = path.parts.index(".cmw-install-staging") + 3
            flushed.add("/".join(path.parts[offset:]))

    monkeypatch.setattr(install_cache, "_flush_path", flush)
    result = _publish(cache_case)
    assert result.created_by_run
    assert result.digest == _digest(cache_case[0], cache_case[2])
    files = {
        path.relative_to(result.cache_path).as_posix() for path in _snapshot(result.cache_path)
    }
    assert files == set(cache_case[2])
    expected_dirs = {str(Path(path).parent).replace("\\", "/") for path in cache_case[2]}
    assert set(cache_case[2]) | expected_dirs <= flushed
    assert capsys.readouterr() == ("", "")


def test_idempotent_publish_preserves_identity_bytes_and_mtimes(cache_case: CacheCase) -> None:
    first = _publish(cache_case)
    before = _snapshot(first.cache_path)
    second = _publish(cache_case)
    after = _snapshot(first.cache_path)
    assert not second.created_by_run
    assert second.identity == first.identity
    assert second.digest == first.digest
    assert after == before


@pytest.mark.parametrize("kind", ["valid", "incomplete", "corrupt"])
def test_unrequested_higher_cache_does_not_select_or_block_requested_generation(
    cache_case: CacheCase,
    kind: str,
) -> None:
    # Given
    _ = _publish(cache_case, "0.0.0")
    higher = _target(cache_case, "9.0.0")
    if kind == "valid":
        _ = _publish(cache_case, "9.0.0")
    else:
        higher.mkdir(parents=True)
        if kind == "corrupt":
            _ = (higher / "unexpected").write_bytes(b"corrupt")

    # When
    requested = _publish(cache_case, "1.0.0")

    # Then
    assert requested.cache_path == _target(cache_case, "1.0.0")
    assert higher.exists()


@pytest.mark.parametrize("version", ["1.10", "v2"])
def test_publish_rejects_noncanonical_versions(cache_case: CacheCase, version: str) -> None:
    with pytest.raises(InstallPluginError, match="package_version_invalid"):
        _ = _publish(cache_case, version)


@pytest.mark.parametrize("kind", PACKAGE_FAILURES)
def test_rejects_invalid_package_boundaries(cache_case: CacheCase, kind: str) -> None:
    manifest = cache_case[0] / MANIFEST
    values = list(cache_case[2])
    replacements = {
        "escape": ["../escape", *values],
        "duplicate": [*values, values[-1]],
        "unsorted": list(reversed(values)),
    }
    if kind in replacements:
        _ = manifest.write_text(json.dumps(replacements[kind]), encoding="utf-8")
    if kind == "bad-json":
        _ = manifest.write_bytes(b"not-json")
    if kind == "bad-hooks":
        _ = (cache_case[0] / "hooks" / "hooks.json").write_bytes(b"not-json")
    if kind == "missing":
        (cache_case[0] / "payload" / "a.txt").unlink()
    version = {
        "bad-version": "local",
        "escape-version": "../escaped",
        "slash-version": "nested/escaped",
    }.get(kind, "1.0.0")
    with pytest.raises(InstallPluginError, match="package_"):
        _ = _publish(cache_case, version)
    assert not _target(cache_case).exists()
    assert not (_target(cache_case).parent / "escaped").exists()


@pytest.mark.parametrize("scenario", PATH_CASES)
def test_path_attacks_fail_closed(
    cache_case: CacheCase,
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(cache_case)
    outside = cache_case[0].parent / "outside"
    source_file = cache_case[0] / "payload" / "a.txt"
    published = scenario.startswith("target-")
    if published:
        _ = _publish(cache_case)
    payload = target / "payload" / "a.txt"
    if scenario == "target-content":
        _ = payload.write_bytes(b"changed")
    if scenario == "target-extra":
        _ = (target / "extra").write_bytes(b"keep")
    if scenario == "target-missing":
        payload.unlink()
    if scenario == "source-symlink":
        _ = outside.write_bytes(b"outside")
        source_file.unlink()
        source_file.symlink_to(outside)

    def changed_open(path: Path, flags: int) -> int:
        descriptor = open_direct_file(path, flags)
        if scenario == "open-race" and path == source_file and not outside.exists():
            os.link(source_file, outside)
        if scenario == "path-swap" and path == source_file and not outside.exists():
            _ = source_file.parent.rename(outside)
            source_file.parent.mkdir()
            _ = source_file.write_bytes(b"replacement")
        return descriptor

    monkeypatch.setattr(install_cache, "open_direct_file", changed_open)
    reason = "installed_generation_conflict" if published else "package_source_unsafe"
    with pytest.raises(InstallPluginError, match=reason):
        _ = _publish(cache_case)
    assert target.exists() is published
