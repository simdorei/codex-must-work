from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts.cache_semver import higher, version_key
from scripts.install_cache import publish_cache
from scripts.install_errors import InstallPluginError

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST = "runtime/package-files.json"
U64_MAX = 18_446_744_073_709_551_615
NON_ASCII = chr(0x03B1)


def _source(root: Path) -> Path:
    files = {
        ".codex-plugin/plugin.json": b'{"name":"fixture"}\n',
        "hooks/hooks.json": b'{"hooks":{}}\n',
        "payload/a.txt": b"A",
    }
    paths = tuple(sorted((*files, MANIFEST), key=str.encode))
    files[MANIFEST] = json.dumps(paths, indent=2).encode() + b"\n"
    for relative, data in files.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)
    return root.resolve()


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (f"{U64_MAX}.{U64_MAX}.{U64_MAX}", True),
        (f"{U64_MAX + 1}.0.0", False),
        (f"{'9' * 5000}.0.0", False),
        ("1.0.0-alpha", True),
        ("1.0.0-alpha.01", False),
        (f"1.0.0-{NON_ASCII}", False),
        ("1.0.0+build_", False),
    ],
)
def test_semver_1_0_27_parse_boundaries(value: str, valid: bool) -> None:
    assert (version_key(value) is not None) is valid


@pytest.mark.parametrize(
    ("candidate", "source", "expected_higher"),
    [
        ("1.0.0+0", "1.0.0", False),
        ("1.0.0", "1.0.0+0", True),
        ("1.0.0+00", "1.0.0+0", True),
        ("1.0.0+1", "1.0.0+00", True),
        ("1.0.0+01", "1.0.0+1", True),
        ("1.0.0+001", "1.0.0+01", True),
        ("1.0.0+2", "1.0.0+001", True),
        ("1.0.0+10", "1.0.0+002", True),
        (f"1.0.0-{NON_ASCII}", "1.0.0-z", True),
        (f"{U64_MAX + 1}.0.0", "9.0.0", False),
    ],
)
def test_semver_1_0_27_order_or_raw_fallback(
    candidate: str,
    source: str,
    expected_higher: bool,
) -> None:
    assert higher(candidate, source) is expected_higher


@pytest.mark.parametrize(
    "version",
    [
        "local",
        ".",
        "..",
        "/absolute",
        "C:\\escape",
        "nested/escape",
        "nested\\escape",
        "NUL",
        "con.txt",
        "name.",
        "name ",
        "bad:name",
        "bad?name",
        "bad|name",
        "bad\tname",
        "bad\0name",
    ],
)
def test_version_path_names_fail_without_escape(tmp_path: Path, version: str) -> None:
    source = _source(tmp_path / "source")
    home = (tmp_path / "home").resolve()
    home.mkdir()
    before = set(tmp_path.rglob("*"))
    with pytest.raises(InstallPluginError, match="package_version_invalid"):
        _ = publish_cache(source, home, version)
    after = set(tmp_path.rglob("*"))
    assert after == before
