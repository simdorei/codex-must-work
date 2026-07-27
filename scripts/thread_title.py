"""Resolve local Codex thread titles without starting an app-server."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import cast, final


@final
class ThreadTitleResolver:
    """Read a Codex thread title from local SQLite state in read-only mode."""

    def __init__(
        self,
        *,
        state_database: Path | None = None,
        codex_home: Path | None = None,
    ) -> None:
        """Pin an explicit database for tests or discover current Codex state."""
        self._state_database = state_database
        self._codex_home = codex_home

    def resolve(self, session_id: str) -> str | None:
        """Return the first matching title without creating or changing a database."""
        for database in self._candidates():
            try:
                uri = f"{database.resolve().as_uri()}?mode=ro"
                with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
                    row = cast(
                        "tuple[object, ...] | None",
                        connection.execute(
                            "SELECT title FROM threads WHERE id = ?",
                            (session_id,),
                        ).fetchone(),
                    )
            except (OSError, sqlite3.Error):
                continue
            if row is not None and isinstance(row[0], str) and row[0].strip():
                return row[0].strip()
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
