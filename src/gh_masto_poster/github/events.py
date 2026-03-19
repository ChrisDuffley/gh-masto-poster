"""Event normalization — merge feed + API events, deduplicate, filter."""

from __future__ import annotations

import logging

from gh_masto_poster.config import EventsConfig
from gh_masto_poster.models import Event, EventType

log = logging.getLogger(__name__)

# Map EventType to the config key in [events] that enables/disables it
_TYPE_TO_CONFIG_KEY: dict[EventType, str] = {
    EventType.RELEASE: "releases",
    EventType.PUSH: "commits",
    EventType.CREATE: "tags",  # tags and branches share CreateEvent
    EventType.DELETE: "branches",
    EventType.ISSUES: "issues",
    EventType.ISSUE_COMMENT: "comments",
    EventType.PULL_REQUEST: "pull_requests",
    EventType.PULL_REQUEST_REVIEW: "reviews",
    EventType.PULL_REQUEST_REVIEW_COMMENT: "reviews",
    EventType.WATCH: "stars",
    EventType.FORK: "forks",
    EventType.COMMIT_COMMENT: "comments",
    EventType.GOLLUM: "wiki",
    EventType.MEMBER: "branches",  # rare, group with misc
    EventType.PUBLIC: "releases",  # rare, group with releases
    EventType.DISCUSSION: "discussions",
    EventType.SECURITY_ADVISORY: "security",
    EventType.CHECK_SUITE: "ci",
    EventType.DEPENDABOT_ALERT: "security",
    EventType.REPOSITORY_INVITATION: "invitations",
}


def merge_and_filter(
    feed_events: list[Event],
    api_events: list[Event],
    notification_events: list[Event],
    events_config: EventsConfig,
) -> list[Event]:
    """Merge events from all sources, deduplicate, filter by config, sort by time.

    Feed events take priority over API events for the same underlying action
    (matched by URL). This implements the feed-first strategy.
    """
    # Collect feed event URLs so we can skip API duplicates
    feed_urls: set[str] = {e.url for e in feed_events if e.url}

    # Start with feed events, then add non-duplicate API events
    merged: dict[str, Event] = {}

    for event in feed_events:
        merged[event.event_id] = event

    for event in api_events:
        if event.event_id in merged:
            continue
        # Skip if feed already covered this URL
        if event.url and event.url in feed_urls:
            log.debug("Skipping API event (covered by feed): %s", event.url)
            continue
        merged[event.event_id] = event

    for event in notification_events:
        if event.event_id not in merged:
            merged[event.event_id] = event

    # Filter by enabled event types
    result: list[Event] = []
    for event in merged.values():
        config_key = _TYPE_TO_CONFIG_KEY.get(event.event_type)
        if config_key and not events_config.enabled.get(config_key, False):
            log.debug("Skipping disabled event type %s (%s)", event.event_type.value, config_key)
            continue

        # Special case: CreateEvent can be branch or tag
        if event.event_type == EventType.CREATE and event.ref_type == "branch":
            if not events_config.enabled.get("branches", False):
                continue

        result.append(event)

    # Sort by creation time (oldest first for chronological posting)
    result.sort(key=lambda e: e.created_at)

    log.info(
        "Merged events: %d feed + %d api + %d notification → %d after dedup/filter",
        len(feed_events),
        len(api_events),
        len(notification_events),
        len(result),
    )
    return result
