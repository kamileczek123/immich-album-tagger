"""Background preparation of suggestions for the next albums.

Generating suggestions takes a few seconds per album, mostly LLM time. While the user
reviews one album, worker threads prepare the next ones, so opening them is instant.
Prepared results live only in memory (they are cheap to recompute).

The *window* is the first ``prefetch.albums`` albums that are not tagged yet, in the same
order as the album list, or as the list the user is looking at (filtered by a search such
as "2024", see ``TaggerService.set_prefetch_focus``). Prepared albums that leave the window
(the filter changed) are kept, up to as many again as the window, so going back is free.
Applying an album removes it from the window, a worker wakes up and prepares the album
that slid in. ``workers`` albums are prepared at the same time (each by its own thread,
never the same album twice). How many LLM requests really run at
once is decided by the LLM pool (llm.py), shared by all workers, also those of other
logins (auth.mode immich_keys); the workers mark their requests as background, so
interactive requests go first.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import PrefetchConfig
from .immich import Album
from .llm import background
from .suggest import SuggestionResult

log = logging.getLogger(__name__)


@dataclass
class Prepared:
    album: Album
    result: SuggestionResult
    created: float


class Prefetcher:
    def __init__(
        self,
        config: PrefetchConfig,
        window: Callable[[int], list[str]],
        compute: Callable[[str], tuple[Album, SuggestionResult]],
        clock: Callable[[], float] = time.monotonic,
        workers: int = 1,
    ):
        """``window(n)`` returns the next n untagged album ids; ``compute`` makes suggestions;
        ``workers`` albums are prepared at the same time."""
        self.config = config
        self._window = window
        self._compute = compute
        self._clock = clock
        self.workers = max(1, workers)
        self._lock = threading.Lock()
        self._cache: dict[str, Prepared] = {}
        self._failed: dict[str, float] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._in_progress: set[str] = set()  # albums being prepared right now
        self.last_window: list[str] = []

    # ------------------------------------------------------------------ cache

    def get(self, album_id: str) -> Prepared | None:
        with self._lock:
            entry = self._cache.get(album_id)
            if entry and not self._fresh(entry):
                del self._cache[album_id]
                return None
            return entry

    def put(self, album_id: str, album: Album, result: SuggestionResult) -> None:
        with self._lock:
            self._cache[album_id] = Prepared(album, result, self._clock())

    def forget(self, album_id: str) -> None:
        """Drop an album (applied or refreshed) and let the worker move on."""
        with self._lock:
            self._cache.pop(album_id, None)
            self._failed.pop(album_id, None)
        self.wake()

    def ready_ids(self) -> set[str]:
        with self._lock:
            return {k for k, v in self._cache.items() if self._fresh(v)}

    def _fresh(self, entry: Prepared) -> bool:
        return self._clock() - entry.created < self.config.max_age_minutes * 60

    # ------------------------------------------------------------------ worker

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> str | None:
        """Prepare at most one album from the window. Returns its id, or None if idle."""
        window = self._window(self.config.albums)
        self.last_window = window
        now = self._clock()
        with self._lock:
            # Keep prepared albums that left the window (the user changed the filter and may
            # come back), but only the newest ones, as many as the window holds.
            outside = sorted(
                (a for a in self._cache if a not in window), key=lambda a: self._cache[a].created
            )
            for album_id in outside[: max(0, len(outside) - self.config.albums)]:
                del self._cache[album_id]
            todo = next(
                (a for a in window if a not in self._in_progress and self._needs_work(a, now)),
                None,
            )
            if todo is None:
                return None
            self._in_progress.add(todo)
        try:
            album, result = self._compute(todo)
        except Exception as exc:  # noqa: BLE001 - background worker must survive anything
            log.warning("Prefetch of album %s failed: %s", todo, exc)
            with self._lock:
                self._failed[todo] = self._clock()
            return todo
        finally:
            with self._lock:
                self._in_progress.discard(todo)
        with self._lock:
            if result.warnings:
                # Typically the LLM was unavailable: keep the rule-based result, retry later.
                self._failed[todo] = self._clock()
            else:
                self._failed.pop(todo, None)
        self.put(todo, album, result)
        return todo

    def _needs_work(self, album_id: str, now: float) -> bool:
        """Caller holds the lock."""
        if now - self._failed.get(album_id, -1e18) < self.config.retry_after_minutes * 60:
            return False
        entry = self._cache.get(album_id)
        return entry is None or not self._fresh(entry) or bool(entry.result.warnings)

    def _loop(self) -> None:
        background.set(True)  # LLM requests of this thread give way to interactive ones
        idle_poll = 60.0
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:  # noqa: BLE001 - e.g. Immich unreachable
                log.warning("Prefetch cycle failed: %s", exc)
                worked = None
            if worked is None:
                self._wake.wait(idle_poll)
                self._wake.clear()

    def start(self) -> None:
        if self._threads:
            return
        for number in range(self.workers):
            thread = threading.Thread(target=self._loop, name=f"prefetch-{number}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def status(self) -> dict:
        ready = self.ready_ids()
        with self._lock:
            in_progress = sorted(self._in_progress)
        return {
            "enabled": True,
            "window": len(self.last_window),
            "ready": sum(1 for a in self.last_window if a in ready),
            "preparing": next(iter(in_progress), None),
            "preparing_count": len(in_progress),
            "workers": self.workers,
        }
