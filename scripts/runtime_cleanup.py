"""Handle-relative POSIX deletion for quarantined runtime trees."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final, Never

from scripts.cache_types import CacheIdentity, identity
from scripts.cache_windows import mark_windows_delete, open_locked

_DIRECTORY_FLAGS: Final = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS: Final = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class RuntimeCleanupError(OSError):
    """Exact runtime cleanup target changed or contained an unsafe object."""


def delete_runtime_tree(root: Path, expected: CacheIdentity) -> None:
    """Delete only one opened directory identity through host-native operations."""
    if os.name == "nt":
        _delete_windows_tree(root, expected)
        return
    _delete_posix_tree(root, expected)


def _delete_posix_tree(root: Path, expected: CacheIdentity) -> None:
    parent = os.open(root.parent, _DIRECTORY_FLAGS)
    try:
        descriptor = os.open(root.name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            if identity(opened) != expected or not stat.S_ISDIR(opened.st_mode):
                _fail()
            _delete_children(descriptor)
            named = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
            if identity(named) != expected:
                _fail()
        finally:
            os.close(descriptor)
        os.rmdir(root.name, dir_fd=parent)
    finally:
        os.close(parent)


def _delete_children(parent: int) -> None:
    for entry in tuple(os.scandir(parent)):
        metadata = entry.stat(follow_symlinks=False)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & reparse:
            _fail()
        if stat.S_ISDIR(metadata.st_mode):
            _delete_directory(parent, entry.name, identity(metadata))
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            _fail()
        child = os.open(entry.name, _FILE_FLAGS, dir_fd=parent)
        try:
            if identity(os.fstat(child)) != identity(metadata):
                _fail()
            named = os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
            if identity(named) != identity(metadata):
                _fail()
            os.unlink(entry.name, dir_fd=parent)
        finally:
            os.close(child)


def _delete_directory(parent: int, name: str, expected: CacheIdentity) -> None:
    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if identity(os.fstat(child)) != expected:
            _fail()
        _delete_children(child)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if identity(named) != expected:
            _fail()
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)


def _delete_windows_tree(root: Path, expected: CacheIdentity) -> None:
    descriptor = open_locked(root, delete_access=True)
    try:
        opened = os.fstat(descriptor)
        if identity(opened) != expected or not stat.S_ISDIR(opened.st_mode):
            _fail()
        for entry in tuple(os.scandir(root)):
            path = Path(entry.path)
            metadata = path.lstat()
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if getattr(metadata, "st_file_attributes", 0) & reparse:
                _fail()
            if stat.S_ISDIR(metadata.st_mode):
                _delete_windows_tree(path, identity(metadata))
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                _fail()
            child = open_locked(path, delete_access=True)
            try:
                if identity(os.fstat(child)) != identity(metadata):
                    _fail()
                mark_windows_delete(child)
            finally:
                os.close(child)
        if identity(os.fstat(descriptor)) != expected:
            _fail()
        mark_windows_delete(descriptor)
    finally:
        os.close(descriptor)


def _fail() -> Never:
    raise RuntimeCleanupError
