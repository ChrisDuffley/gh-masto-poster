"""Tests for the event normalizer."""

from datetime import datetime, timezone

from gh_masto_poster.config import EventsConfig
from gh_masto_poster.github.events import merge_and_filter
from gh_masto_poster.models import Event, EventSource, EventType


def _evt(
    event_type: EventType = EventType.RELEASE,
    source: EventSource = EventSource.FEED,
    repo: str = "a/b",
    url: str = "",
    event_id: str = "",
    **kwargs,
) -> Event:
    return Event(
        event_type=event_type,
        source=source,
        repo=repo,
        title=kwargs.get("title", "test"),
        url=url or f"https://example.com/{event_id or 'x'}",
        created_at=kwargs.get("created_at", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        event_id=event_id or "",
        **{k: v for k, v in kwargs.items() if k not in ("title", "created_at")},
    )


def test_feed_preferred_over_api() -> None:
    """Feed events should take priority over API events for the same URL."""
    feed = [_evt(source=EventSource.FEED, url="https://example.com/release/1", event_id="f1")]
    api = [_evt(source=EventSource.API, url="https://example.com/release/1", event_id="a1")]

    result = merge_and_filter(feed, api, [], EventsConfig())
    assert len(result) == 1
    assert result[0].source == EventSource.FEED


def test_api_only_events_included() -> None:
    """API-only events (e.g., stars) are included when enabled."""
    config = EventsConfig(enabled={"stars": True, "releases": True})
    feed = [_evt(event_id="f1")]
    api = [_evt(event_type=EventType.WATCH, source=EventSource.API, url="https://example.com/star", event_id="a1")]

    result = merge_and_filter(feed, api, [], config)
    assert len(result) == 2


def test_disabled_events_filtered() -> None:
    """Disabled event types are filtered out."""
    config = EventsConfig(enabled={"releases": False, "stars": False})
    events = [
        _evt(event_type=EventType.RELEASE, event_id="r1"),
        _evt(event_type=EventType.WATCH, source=EventSource.API, url="https://example.com/star", event_id="s1"),
    ]

    result = merge_and_filter(events, [], [], config)
    assert len(result) == 0


def test_sorted_by_time() -> None:
    """Events should be sorted oldest first."""
    e1 = _evt(event_id="e1", created_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
    e2 = _evt(event_id="e2", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), url="https://other.com/2")

    result = merge_and_filter([e1, e2], [], [], EventsConfig())
    assert result[0].event_id == e2.event_id
    assert result[1].event_id == e1.event_id


def test_notifications_included() -> None:
    """Notification events are included in merge."""
    notif = [_evt(
        event_type=EventType.SECURITY_ADVISORY,
        source=EventSource.NOTIFICATION,
        url="https://example.com/advisory",
        event_id="n1",
    )]
    result = merge_and_filter([], [], notif, EventsConfig())
    assert len(result) == 1
    assert result[0].source == EventSource.NOTIFICATION


def test_dedup_by_event_id() -> None:
    """Events with the same event_id are deduplicated."""
    e1 = _evt(source=EventSource.FEED, event_id="same")
    e2 = _evt(source=EventSource.API, event_id="same", url="https://different.com")

    result = merge_and_filter([e1], [e2], [], EventsConfig())
    assert len(result) == 1
