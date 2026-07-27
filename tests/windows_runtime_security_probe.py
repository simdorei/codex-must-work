"""Public-safe phase diagnostics for the Windows runtime security contract."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts import cache_windows
from scripts.cache_types import CacheIdentity, identity
from scripts.windows_file import final_windows_path

_ACCESS_FLAGS = 0x00020000 | 0x80 | 0x8
_SHARE_FLAGS = 3
_OPEN_FLAGS = 0x02000000 | 0x00200000
type DescriptorCheck = Callable[[int], bool]
type PathCheck = Callable[[Path, bool], bool]
type SecurityText = Callable[[int], str]
type ExpectedSecurityText = Callable[[bool], str]
type NormalizePath = Callable[[Path], str]


@dataclass(frozen=True, slots=True)
class WindowsPathProbe:
    relative_path: str
    ok: bool
    phase: str
    winerror: int | None
    access_flags: int
    share_flags: int
    open_flags: int
    opened_identity: CacheIdentity | None
    named_before: CacheIdentity | None
    named_after: CacheIdentity | None


class _ProbeState:
    """Accumulate phase evidence while one path probe advances."""

    __slots__: tuple[str, ...] = (
        "named_after",
        "named_before",
        "opened_identity",
        "relative_path",
    )

    def __init__(self, relative_path: str) -> None:
        self.relative_path: str = relative_path
        self.opened_identity: CacheIdentity | None = None
        self.named_before: CacheIdentity | None = None
        self.named_after: CacheIdentity | None = None


def inspect_windows_runtime_path(
    root: Path,
    relative_path: str,
    *,
    directory: bool,
) -> WindowsPathProbe:
    """Evaluate each Windows path-policy phase on one locked handle."""
    path = root.joinpath(*relative_path.split("/"))
    state = _ProbeState(relative_path)
    phase = "open_locked"
    descriptor: int | None = None
    try:
        state.named_before = identity(path.lstat())
        descriptor = cache_windows.open_locked(path)
        opened = os.fstat(descriptor)
        state.opened_identity = identity(opened)
        named = path.lstat()
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        checks = (
            ("direct_kind", expected_kind(opened.st_mode)),
            ("identity", state.opened_identity == identity(named)),
            ("single_link", directory or opened.st_nlink == 1),
            ("final_path", _normalized()(final_windows_path(descriptor)) == _normalized()(path)),
            (
                "attributes",
                getattr(opened, "st_file_attributes", 0) in ({0x10} if directory else {0x20, 0x80}),
            ),
            (
                "reparse",
                not getattr(opened, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0),
            ),
            (
                "acl",
                _security_text()(descriptor) == _expected_security_text()(directory),
            ),
            ("streams", _plain_streams()(path, directory)),
            ("extended_attributes", _empty_ea()(descriptor)),
        )
        for phase, passed in checks:
            if not passed:
                state.named_after = identity(path.lstat())
                return _result(
                    state,
                    ok=False,
                    phase=phase,
                    winerror=None,
                )
        state.named_after = identity(path.lstat())
        return _result(
            state,
            ok=state.opened_identity == state.named_after,
            phase="ok" if state.opened_identity == state.named_after else "post_identity",
            winerror=None,
        )
    except OSError as error:
        return _result(
            state,
            ok=False,
            phase=phase,
            winerror=getattr(error, "winerror", None),
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _result(
    state: _ProbeState,
    *,
    ok: bool,
    phase: str,
    winerror: int | None,
) -> WindowsPathProbe:
    return WindowsPathProbe(
        state.relative_path,
        ok,
        phase,
        winerror,
        _ACCESS_FLAGS,
        _SHARE_FLAGS,
        _OPEN_FLAGS,
        state.opened_identity,
        state.named_before,
        state.named_after,
    )


def _normalized() -> NormalizePath:
    return cast("NormalizePath", vars(cache_windows)["_normalized"])


def _security_text() -> SecurityText:
    return cast("SecurityText", vars(cache_windows)["_security_sddl"])


def _expected_security_text() -> ExpectedSecurityText:
    return cast("ExpectedSecurityText", vars(cache_windows)["_expected_sddl"])


def _plain_streams() -> PathCheck:
    return cast("PathCheck", vars(cache_windows)["_plain_streams"])


def _empty_ea() -> DescriptorCheck:
    return cast("DescriptorCheck", vars(cache_windows)["_empty_ea"])
