"""Tests for the State module."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from gh_masto_poster.state import State, _MAX_CACHED_EVENTS


def test_fresh_state(tmp_path: Path) -> None:
    state = State(tmp_path / "state.json")
    assert not state.has_event("abc")


def test_record_and_check(tmp_path: Path) -> None:
    state = State(tmp_path / "state.json")
    state.record_event("evt1")
    assert state.has_event("evt1")
    assert not state.has_event("evt2")


def test_save_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State(path)
    state.record_event("e1")
    state.set_feed_etag("https://example.com/feed", '"abc123"')
    state.set_api_etag("/repos/a/b/events", '"def456"')
    state.touch_poll()
    state.save()

    state2 = State(path)
    assert state2.has_event("e1")
    assert state2.get_feed_etag("https://example.com/feed") == '"abc123"'
    assert state2.get_api_etag("/repos/a/b/events") == '"def456"'


def test_prune_keeps_most_recent(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    # Create more events than the limit
    posted = {}
    for i in range(_MAX_CACHED_EVENTS + 50):
        ts = f"2026-01-{i // 28 + 1:02d}T{i % 24:02d}:{i % 60:02d}:00+00:00"
        posted[f"evt_{i:04d}"] = ts
    # Add a known newest event
    posted["newest"] = "2026-12-31T23:59:59+00:00"
    # Add a known oldest event
    posted["oldest"] = "2025-01-01T00:00:00+00:00"

    data = {
        "last_poll": "",
        "posted_events": posted,
        "feed_etags": {},
        "api_etags": {},
    }
    with open(path, "w") as f:
        json.dump(data, f)

    state = State(path)
    state.save()  # triggers prune

    state2 = State(path)
    # Should have been pruned down to _MAX_CACHED_EVENTS
    assert len(state2._data["posted_events"]) == _MAX_CACHED_EVENTS
    # Newest should survive
    assert state2.has_event("newest")
    # Oldest should have been pruned
    assert not state2.has_event("oldest")


def test_atomic_write(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State(path)
    state.record_event("x")
    state.save()
    assert path.exists()

    # Verify valid JSON
    with open(path) as f:
        data = json.load(f)
    assert "x" in data["posted_events"]

    # No leftover temp files
    temps = list(tmp_path.glob(".state_*.tmp"))
    assert len(temps) == 0


def test_corrupt_state_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not json{{{")

    state = State(path)  # should not raise
    assert not state.has_event("anything")
