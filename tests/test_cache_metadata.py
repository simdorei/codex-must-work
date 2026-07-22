from __future__ import annotations

import ctypes
import json
import os
import stat
from pathlib import Path
from typing import Protocol, cast

import pytest

from scripts.cache_windows import secure_windows_path
from scripts.install_cache import publish_cache
from scripts.install_errors import InstallPluginError
from scripts.windows_file import open_windows_path, windows_handle

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows metadata contract")
MANIFEST = "runtime/package-files.json"


class _WinCall(Protocol):
    def __call__(self, *arguments: object) -> int | None: ...


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


def _publish(tmp_path: Path) -> tuple[Path, Path, Path]:
    source, home = _source(tmp_path / "source"), (tmp_path / "home").resolve()
    home.mkdir()
    target = publish_cache(source, home, "1.0.0").cache_path
    return source, home, target


def _set_ea(path: Path) -> None:
    descriptor = open_windows_path(
        path,
        0x10,
        attributes=0x02000000,
        descriptor_flags=os.O_RDWR,
    )
    name, value = b"cmw-test", b"x"
    data = bytearray(8 + len(name) + 1 + len(value))
    data[5] = len(name)
    data[6:8] = len(value).to_bytes(2, "little")
    data[8 : 8 + len(name)] = name
    data[9 + len(name) :] = value
    buffer = (ctypes.c_ubyte * len(data)).from_buffer(data)
    status = (ctypes.c_void_p * 2)()
    setter = cast("_WinCall", ctypes.WinDLL("ntdll").NtSetEaFile)
    try:
        result = setter(
            windows_handle(descriptor),
            ctypes.byref(status),
            ctypes.byref(buffer),
            len(data),
        )
        assert result == 0
    finally:
        os.close(descriptor)


def _weaken_dacl(path: Path) -> None:
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = cast("_WinCall", api.ConvertStringSecurityDescriptorToSecurityDescriptorW)
    get_dacl = cast("_WinCall", api.GetSecurityDescriptorDacl)
    set_security = cast("_WinCall", api.SetSecurityInfo)
    local_free = cast("_WinCall", ctypes.WinDLL("kernel32").LocalFree)
    security, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = ctypes.c_int(), ctypes.c_int()
    assert convert(ctypes.c_wchar_p("D:AI(A;;FA;;;WD)"), 1, ctypes.byref(security), None)
    access = 0x00020000 | 0x00040000 | 0x00080000 | 0x80 | 0x100 | 0x8
    descriptor = open_windows_path(path, access, attributes=0x02000000)
    try:
        assert get_dacl(
            security,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        )
        result = set_security(
            windows_handle(descriptor),
            1,
            0x00000004,
            None,
            None,
            dacl,
            None,
        )
        assert result == 0
    finally:
        os.close(descriptor)
        _ = local_free(security)


def _set_hidden(path: Path) -> None:
    setter = cast("_WinCall", ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW)
    assert setter(ctypes.c_wchar_p(str(path)), 0x2)


def test_published_tree_has_exact_real_windows_policy(tmp_path: Path) -> None:
    _, _, target = _publish(tmp_path)
    paths = (target, *target.rglob("*"))
    for path in paths:
        directory = path.is_dir()
        assert secure_windows_path(path, directory=directory, apply=False)
        expected = stat.FILE_ATTRIBUTE_DIRECTORY if directory else stat.FILE_ATTRIBUTE_NORMAL
        assert path.lstat().st_file_attributes == expected


@pytest.mark.parametrize("object_kind", ["file", "directory"])
@pytest.mark.parametrize("mutation", ["ads", "ea", "dacl", "hidden"])
def test_real_windows_metadata_mutation_is_rejected(
    tmp_path: Path,
    object_kind: str,
    mutation: str,
) -> None:
    source, home, target = _publish(tmp_path)
    path = target / "payload" / ("a.txt" if object_kind == "file" else "")
    if mutation == "ads":
        _ = Path(f"{path}:hidden").write_bytes(b"hidden")
    elif mutation == "ea":
        _set_ea(path)
    elif mutation == "dacl":
        _weaken_dacl(path)
    else:
        _set_hidden(path)
    with pytest.raises(InstallPluginError, match="cache_same_version_mismatch"):
        _ = publish_cache(source, home, "1.0.0")
    assert target.exists()
