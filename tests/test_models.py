"""Tests for the models module."""

from datetime import datetime, timezone

from gh_masto_poster.models import Event, EventSource, EventType, RepoInfo, _truncate


def test_repo_full_name() -> None:
    repo = RepoInfo(owner="alice", name="myrepo")
    assert repo.full_name == "alice/myrepo"


def test_event_auto_id() -> None:
    e = Event(
        event_type=EventType.RELEASE,
        source=EventSource.FEED,
        repo="alice/myrepo",
        title="v1.0",
        url="https://github.com/alice/myrepo/releases/tag/v1.0",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert e.event_id  # auto-generated
    assert len(e.event_id) == 16


def test_event_explicit_id() -> None:
    e = Event(
        event_type=EventType.PUSH,
        source=EventSource.API,
        repo="bob/proj",
        title="commit",
        url="https://example.com",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        event_id="my_custom_id",
    )
    assert e.event_id == "my_custom_id"


def test_event_naive_datetime_gets_utc() -> None:
    e = Event(
        event_type=EventType.ISSUES,
        source=EventSource.API,
        repo="a/b",
        title="t",
        url="",
        created_at=datetime(2026, 1, 1),  # naive
    )
    assert e.created_at.tzinfo is not None


def test_template_vars() -> None:
    e = Event(
        event_type=EventType.RELEASE,
        source=EventSource.FEED,
        repo="alice/myrepo",
        title="v2.0",
        url="https://example.com/release",
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        actor="alice",
        action="published",
        body="Release notes here " * 20,
        ref="v2.0",
        ref_type="tag",
    )
    v = e.to_template_vars()
    assert v["repo"] == "alice/myrepo"
    assert v["tag"] == "v2.0"
    assert v["actor"] == "alice"
    assert len(v["body_truncated"]) <= 200


def test_truncate() -> None:
    assert _truncate("hello", 10) == "hello"
    assert _truncate("hello world", 5) == "hell…"
    assert _truncate("", 5) == ""
