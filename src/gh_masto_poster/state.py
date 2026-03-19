"""Persistent state management — deduplication, ETags, atomic JSON writes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Keep only the most recent N event records for deduplication
_MAX_CACHED_EVENTS = 200


class State:
    """Manages the JSON state file for deduplication and ETag caching."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: dict = {
            "last_poll": "",
            "posted_events": {},
            "feed_etags": {},
            "api_etags": {},
        }
        self._load()

    # ── Public API ──────────────────────────────────────────────

    def has_event(self, event_id: str) -> bool:
        """Check whether an event has already been posted."""
        return event_id in self._data["posted_events"]

    def record_event(self, event_id: str) -> None:
        """Mark an event as posted."""
        self._data["posted_events"][event_id] = _now_iso()

    def get_feed_etag(self, url: str) -> str | None:
        return self._data["feed_etags"].get(url)

    def set_feed_etag(self, url: str, etag: str) -> None:
        self._data["feed_etags"][url] = etag

    def get_api_etag(self, endpoint: str) -> str | None:
        return self._data["api_etags"].get(endpoint)

    def set_api_etag(self, endpoint: str, etag: str) -> None:
        self._data["api_etags"][endpoint] = etag

    def touch_poll(self) -> None:
        """Update the last-poll timestamp."""
        self._data["last_poll"] = _now_iso()

    def save(self) -> None:
        """Atomically write state to disk (write tmp + rename)."""
        self._prune()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=".state_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            # Clean up temp file on failure
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        log.debug("State saved to %s (%d events tracked)", self._path, len(self._data["posted_events"]))

    # ── Internal ────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            log.info("No state file at %s — starting fresh", self._path)
            return
        try:
            with self._path.open() as f:
                loaded = json.load(f)
            # Merge with defaults so missing keys don't cause errors
            for key in self._data:
                if key in loaded:
                    self._data[key] = loaded[key]
            log.info("Loaded state: %d tracked events", len(self._data["posted_events"]))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load state file %s: %s — starting fresh", self._path, exc)

    def _prune(self) -> None:
        """Keep only the most recent _MAX_CACHED_EVENTS entries."""
        posted = self._data["posted_events"]
        if len(posted) <= _MAX_CACHED_EVENTS:
            return
        # Sort by timestamp, keep newest
        sorted_items = sorted(posted.items(), key=lambda kv: kv[1], reverse=True)
        pruned = len(posted) - _MAX_CACHED_EVENTS
        self._data["posted_events"] = dict(sorted_items[:_MAX_CACHED_EVENTS])
        log.info("Pruned %d old event records (keeping %d)", pruned, _MAX_CACHED_EVENTS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
