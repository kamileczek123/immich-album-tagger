"""Operation log: one JSON line per user action, in LOG_DIR/operations.log.

Every Apply, Share and Remove access (one line per album and person) is written with its
outcome, so problems can be looked up after the notification is gone. The file is created
when missing and appended to across restarts; *Clear* in the UI empties it.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SUCCESS = "success"
ERROR = "error"

# Who is acting: set per request by the web app (logged-in user), empty for the CLI.
actor: ContextVar[str] = ContextVar("actor", default="")


class OperationLog:
    def __init__(self, directory: Path, filename: str = "operations.log"):
        self.path = Path(directory) / filename
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        except OSError as exc:
            log.error("Cannot create operation log %s: %s", self.path, exc)

    def add(
        self,
        status: str,
        action: str,
        album_id: str | None = None,
        album_name: str | None = None,
        details: dict[str, Any] | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            "action": action,
            "album_id": album_id,
            "album_name": album_name,
            "details": details or {},
            "message": message,
            "by": actor.get(),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                log.error("Cannot write operation log %s: %s", self.path, exc)
        return entry

    def entries(self, limit: int = 500) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        result = []
        for line in reversed(lines):
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(result) >= limit:
                break
        return result

    def clear(self) -> None:
        with self._lock:
            self.path.write_text("", encoding="utf-8")
