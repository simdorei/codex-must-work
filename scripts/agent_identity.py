"""Resolve privacy-safe display labels for main and subagents."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Final, final

from scripts.notifications import NotificationSubject, NotificationSubjectKind

_FALLBACK_HASH_CHARS: Final = 6
_METADATA_QUERY: Final = " ".join(  # noqa: FLY002
    (
        "SELECT 1 FROM threads WHERE id = ?",
        "AND cmw_agent_label(agent_nickname, agent_role) = 0",
    )
)
type _SqlValue = bytes | float | int | str | None


@final
class _MetadataCollector:
    """Collect one normalized label through a typed SQLite callback."""

    def __init__(self) -> None:
        self.label: str | None = None

    def __call__(self, nickname_value: _SqlValue, role_value: _SqlValue) -> int:
        """Normalize only text metadata and ignore unexpected database types."""
        nickname = nickname_value.strip() if type(nickname_value) is str else ""
        role = role_value.strip() if type(role_value) is str else ""
        if nickname and role:
            self.label = f"{nickname} ({role})"
        elif nickname:
            self.label = nickname
        elif role:
            self.label = f"서브에이전트 ({role})"
        return 0


@final
class AgentIdentityResolver:
    """Read optional Codex agent metadata without starting an app-server."""

    def __init__(
        self,
        *,
        state_database: Path | None = None,
        codex_home: Path | None = None,
    ) -> None:
        """Pin one database for tests or discover current Codex state files."""
        self._state_database = state_database
        self._codex_home = codex_home

    def resolve(self, subject: NotificationSubject) -> str:
        """Return a clear target label while hashing unresolved opaque IDs."""
        match subject.kind:  # noqa: MATCH_OK - StrEnum union is exhaustive.
            case NotificationSubjectKind.TASK:
                return "전체 작업"
            case NotificationSubjectKind.MAIN_AGENT:
                return "메인 에이전트"
            case NotificationSubjectKind.SUBAGENT:
                target_id = subject.target_id
                if target_id is None:
                    raise AssertionError
                metadata = self._metadata(target_id)
                return metadata if metadata is not None else _fallback(target_id)

    def _metadata(self, target_id: str) -> str | None:
        for database in self._candidates():
            try:
                uri = f"{database.resolve().as_uri()}?mode=ro"
                with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
                    collector = _MetadataCollector()
                    _ = connection.create_function(
                        "cmw_agent_label",
                        2,
                        collector,
                    )
                    _cursor = connection.execute(_METADATA_QUERY, (target_id,))
            except (OSError, sqlite3.Error):
                continue
            if collector.label is not None:
                return collector.label
        return None

    def _candidates(self) -> tuple[Path, ...]:
        if self._state_database is not None:
            return (self._state_database,)
        configured = os.environ.get("CODEX_STATE_DB")
        if configured:
            return (Path(configured),)
        home = self._codex_home
        if home is None:
            home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        try:
            return tuple(
                sorted(
                    home.glob("state_*.sqlite"),
                    key=lambda path: path.stat().st_mtime_ns,
                    reverse=True,
                )
            )
        except OSError:
            return ()


def _fallback(target_id: str) -> str:
    digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()
    return f"서브에이전트 #{digest[:_FALLBACK_HASH_CHARS]}"
