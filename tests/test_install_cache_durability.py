from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import install_cache
from scripts.install_cache import CachePublication, publish_cache
from scripts.install_errors import InstallPluginError

type CacheCase = tuple[Path, Path]

_MANIFEST = "runtime/package-files.json"
_DURABILITY_CASES = (
    "file",
    "directory",
    "final-parent",
    "post-rename",
    "no-replace",
    "cleanup-swap",
)


def _source(root: Path) -> None:
    selected = {
        ".codex-plugin/plugin.json": b'{"name":"fixture"}\n',
        "hooks/hooks.json": b'{"hooks":{}}\n',
        "payload/a.txt": b"A",
    }
    paths = tuple(sorted((*selected, _MANIFEST), key=str.encode))
    selected[_MANIFEST] = json.dumps(paths, indent=2).encode() + b"\n"
    for relative, data in selected.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)


@pytest.fixture
def cache_case(tmp_path: Path) -> CacheCase:
    source = (tmp_path / "source").resolve()
    home = (tmp_path / "home").resolve()
    _source(source)
    home.mkdir()
    return source, home


def _publish(case: CacheCase, version: str = "1.0.0") -> CachePublication:
    return publish_cache(case[0], case[1], version)


def _target(case: CacheCase) -> Path:
    return case[1] / "plugins" / "cache" / "codex-must-work-local" / "codex-must-work" / "1.0.0"


@pytest.mark.parametrize("scenario", _DURABILITY_CASES)
def test_durability_failure_rolls_back_only_run_identity(
    cache_case: CacheCase,
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = _publish(cache_case, "0.0.0")
    target = _target(cache_case)

    def changed_flush(path: Path) -> None:
        staging = ".cmw-install-staging" in path.parts
        if scenario == "file" and staging and path.is_file():
            raise OSError
        if scenario == "directory" and staging and path.is_dir():
            raise OSError
        final_failure = scenario in {"final-parent", "cleanup-swap"}
        if final_failure and path == target.parent and target.exists():
            if scenario == "cleanup-swap":
                _ = target.rename(cache_case[0].parent / "published-old")
                target.mkdir()
                _ = (target / "competitor").write_bytes(b"keep")
            raise OSError

    def changed_rename(stage: Path, destination: Path) -> None:
        if scenario == "no-replace":
            destination.mkdir()
            _ = (destination / "competitor").write_bytes(b"keep")
            raise FileExistsError
        _ = stage.rename(destination)
        if scenario == "post-rename":
            _ = (destination / "payload" / "a.txt").write_bytes(b"tampered")

    monkeypatch.setattr(install_cache, "_flush_path", changed_flush)
    monkeypatch.setattr(install_cache, "_rename_no_replace", changed_rename)
    reason = "cache_cleanup_failed" if scenario == "cleanup-swap" else "cache_"
    with pytest.raises(InstallPluginError, match=reason):
        _ = _publish(cache_case)
    assert prior.cache_path.exists()
    if scenario in {"no-replace", "cleanup-swap"}:
        assert (target / "competitor").read_bytes() == b"keep"
    else:
        assert not target.exists()
