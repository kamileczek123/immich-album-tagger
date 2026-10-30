"""Local record of what was applied to which album (SQLite in DATA_DIR).

Immich has no "album tags", so this is how the UI knows an album was already processed.
Deleting the file only resets the "Tagged" markers; data in Immich is untouched.

Each entry records the Immich user who applied it (``user_id``). Entries from before 0.6.0
have none; they count for everyone until :meth:`History.claim_unowned` gives them to a user.

The state of an album (:meth:`History.latest_by_album`) is what the app put there and did
not take back: the tags of every run, minus those a later run removed (``removed``, since
0.9.0 Apply removes tags taken out of the selection); a ``remove`` run clears the album.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id     TEXT    NOT NULL,
    album_name   TEXT    NOT NULL,
    tags         TEXT    NOT NULL,
    asset_count  INTEGER NOT NULL,
    new_links    INTEGER NOT NULL,
    verified     INTEGER,
    applied_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_album ON applications (album_id);
"""

# Columns added after 0.1.0; added in place to existing databases.
_MIGRATIONS = {
    "keywords": "ALTER TABLE applications ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]'",
    "targets": "ALTER TABLE applications ADD COLUMN targets TEXT NOT NULL DEFAULT '{}'",
    "user_id": "ALTER TABLE applications ADD COLUMN user_id TEXT",
    "removed": "ALTER TABLE applications ADD COLUMN removed TEXT NOT NULL DEFAULT '[]'",
    "action": "ALTER TABLE applications ADD COLUMN action TEXT NOT NULL DEFAULT 'apply'",
}


class History:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
            for column, statement in _MIGRATIONS.items():
                if column not in columns:
                    conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        album_id: str,
        album_name: str,
        tags: list[str],
        keywords: list[str],
        targets: dict[str, bool],
        asset_count: int,
        new_links: int,
        verified: bool | None,
        user_id: str | None = None,
        removed: list[str] | None = None,
        action: str = "apply",
    ) -> dict[str, Any]:
        """One run: ``apply`` (tags written, ``removed`` taken off) or ``remove`` (all)."""
        applied_at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO applications (album_id, album_name, tags, keywords, targets,"
                " asset_count, new_links, verified, applied_at, user_id, removed, action)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    album_id,
                    album_name,
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(keywords, ensure_ascii=False),
                    json.dumps(targets),
                    asset_count,
                    new_links,
                    None if verified is None else int(verified),
                    applied_at,
                    user_id,
                    json.dumps(removed or [], ensure_ascii=False),
                    action,
                ),
            )
        return {
            "removed": removed or [],
            "tags": tags,
            "keywords": keywords,
            "targets": targets,
            "applied_at": applied_at,
            "asset_count": asset_count,
            "verified": verified,
        }

    def claim_unowned(self, user_id: str) -> int:
        """Give entries without a user (from before 0.6.0) to ``user_id``. Returns how many."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE applications SET user_id = ? WHERE user_id IS NULL", (user_id,)
            )
            return cursor.rowcount

    def latest_by_album(self, user_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Per album: the tags the app put there and did not remove, the keywords of the
        latest album-description run, and the latest run's details. Albums whose latest
        run was ``remove`` are left out (not tagged).

        With ``user_id``: only that user's entries and those without a user.
        """
        query = (
            "SELECT album_id, tags, keywords, targets, asset_count, verified, applied_at,"
            " removed, action FROM applications"
        )
        params: tuple[str, ...] = ()
        if user_id is not None:
            query += " WHERE user_id = ? OR user_id IS NULL"
            params = (user_id,)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY id", params).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["action"] == "remove":
                result.pop(row["album_id"], None)
                continue
            entry = result.setdefault(row["album_id"], {"tags": [], "keywords": []})
            for value in json.loads(row["tags"]):
                if value.casefold() not in {t.casefold() for t in entry["tags"]}:
                    entry["tags"].append(value)
            removed = {value.casefold() for value in json.loads(row["removed"])}
            entry["tags"] = [t for t in entry["tags"] if t.casefold() not in removed]
            keywords = json.loads(row["keywords"])
            if keywords:  # the album line is replaced, so the latest keywords are current
                entry["keywords"] = keywords
            entry.update(
                targets=json.loads(row["targets"]),
                applied_at=row["applied_at"],
                asset_count=row["asset_count"],
                verified=None if row["verified"] is None else bool(row["verified"]),
            )
        return result
