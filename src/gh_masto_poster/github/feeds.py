"""Atom feed fetcher for GitHub releases, commits, tags, and user activity."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx

from gh_masto_poster.models import Event, EventSource, EventType, RepoInfo
from gh_masto_poster.state import State

log = logging.getLogger(__name__)

# Feed URL templates
_RELEASES_FEED = "https://github.com/{owner}/{repo}/releases.atom"
_COMMITS_FEED = "https://github.com/{owner}/{repo}/commits/{branch}.atom"
_TAGS_FEED = "https://github.com/{owner}/{repo}/tags.atom"
_USER_FEED = "https://github.com/{username}.atom"

# Map event type strings found in user feed <id> tags to our EventType
_FEED_EVENT_TYPE_MAP: dict[str, EventType] = {
    "PushEvent": EventType.PUSH,
    "CreateEvent": EventType.CREATE,
    "DeleteEvent": EventType.DELETE,
    "ReleaseEvent": EventType.RELEASE,
    "IssuesEvent": EventType.ISSUES,
    "IssueCommentEvent": EventType.ISSUE_COMMENT,
    "PullRequestEvent": EventType.PULL_REQUEST,
    "PullRequestReviewEvent": EventType.PULL_REQUEST_REVIEW,
    "PullRequestReviewCommentEvent": EventType.PULL_REQUEST_REVIEW_COMMENT,
    "WatchEvent": EventType.WATCH,
    "ForkEvent": EventType.FORK,
    "CommitCommentEvent": EventType.COMMIT_COMMENT,
    "GollumEvent": EventType.GOLLUM,
    "MemberEvent": EventType.MEMBER,
    "PublicEvent": EventType.PUBLIC,
}

# Pattern to extract event type from user feed <id> tags
# e.g. "tag:github.com,2008:PushEvent/12345678"
_ID_EVENT_TYPE_RE = re.compile(r"tag:github\.com,\d+:(\w+Event)/\d+")


async def fetch_feed_events(
    client: httpx.AsyncClient,
    repo: RepoInfo,
    state: State,
    *,
    releases: bool = True,
    commits: bool = True,
    tags: bool = True,
) -> list[Event]:
    """Fetch Atom feeds for a repo and return new events not yet in state."""
    events: list[Event] = []

    feeds: list[tuple[str, EventType]] = []
    if releases:
        url = _RELEASES_FEED.format(owner=repo.owner, repo=repo.name)
        feeds.append((url, EventType.RELEASE))
    if commits:
        url = _COMMITS_FEED.format(owner=repo.owner, repo=repo.name, branch=repo.default_branch)
        feeds.append((url, EventType.PUSH))
    if tags:
        url = _TAGS_FEED.format(owner=repo.owner, repo=repo.name)
        feeds.append((url, EventType.CREATE))

    for feed_url, event_type in feeds:
        try:
            new_events = await _fetch_single_feed(client, feed_url, event_type, repo, state)
            events.extend(new_events)
        except Exception:
            log.exception("Failed to fetch feed %s", feed_url)

    return events


async def fetch_user_feed_events(
    client: httpx.AsyncClient,
    username: str,
    state: State,
) -> list[Event]:
    """Fetch the user's activity Atom feed and return new events."""
    url = _USER_FEED.format(username=username)

    headers: dict[str, str] = {}
    etag = state.get_feed_etag(url)
    if etag:
        headers["If-None-Match"] = etag

    try:
        resp = await client.get(url, headers=headers, follow_redirects=True)
    except Exception:
        log.exception("Failed to fetch user feed %s", url)
        return []

    if resp.status_code == 304:
        log.debug("User feed unchanged (304): %s", url)
        return []

    if resp.status_code != 200:
        log.warning("User feed fetch failed (%d): %s", resp.status_code, url)
        return []

    new_etag = resp.headers.get("etag")
    if new_etag:
        state.set_feed_etag(url, new_etag)

    parsed = feedparser.parse(resp.text)
    events: list[Event] = []

    for entry in parsed.entries:
        entry_id = entry.get("id") or entry.get("link", "")
        if not entry_id or state.has_event(entry_id):
            continue

        event = _user_entry_to_event(entry, entry_id, username)
        if event:
            events.append(event)

    log.info("User feed %s: %d new entries", url, len(events))
    return events


async def _fetch_single_feed(
    client: httpx.AsyncClient,
    url: str,
    event_type: EventType,
    repo: RepoInfo,
    state: State,
) -> list[Event]:
    """Fetch one Atom feed, parse entries, and return new Events."""
    headers: dict[str, str] = {}
    etag = state.get_feed_etag(url)
    if etag:
        headers["If-None-Match"] = etag

    resp = await client.get(url, headers=headers, follow_redirects=True)

    if resp.status_code == 304:
        log.debug("Feed unchanged (304): %s", url)
        return []

    if resp.status_code != 200:
        log.warning("Feed fetch failed (%d): %s", resp.status_code, url)
        return []

    # Store ETag for next request
    new_etag = resp.headers.get("etag")
    if new_etag:
        state.set_feed_etag(url, new_etag)

    parsed = feedparser.parse(resp.text)
    events: list[Event] = []

    for entry in parsed.entries:
        entry_id = entry.get("id") or entry.get("link", "")
        if not entry_id or state.has_event(entry_id):
            continue

        event = _entry_to_event(entry, event_type, repo, entry_id)
        if event:
            events.append(event)

    log.info("Feed %s: %d new entries", url, len(events))
    return events


def _entry_to_event(
    entry: feedparser.FeedParserDict,
    event_type: EventType,
    repo: RepoInfo,
    entry_id: str,
) -> Event | None:
    """Convert a feedparser entry to a unified Event."""
    title = entry.get("title", "")
    link = entry.get("link", "")
    summary = entry.get("summary", "")
    author = entry.get("author", "")

    # Parse published/updated time
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct:
        created_at = datetime.fromtimestamp(mktime(time_struct), tz=timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    if event_type == EventType.RELEASE:
        # Extract tag name from title (GitHub format: "Release vX.Y.Z" or just tag)
        return Event(
            event_type=EventType.RELEASE,
            source=EventSource.FEED,
            repo=repo.full_name,
            title=title,
            url=link,
            created_at=created_at,
            actor=author,
            action="published",
            body=summary,
            ref=title,  # Often the tag name
            ref_type="tag",
            event_id=entry_id,
        )

    if event_type == EventType.PUSH:
        return Event(
            event_type=EventType.PUSH,
            source=EventSource.FEED,
            repo=repo.full_name,
            title=title,
            url=link,
            created_at=created_at,
            actor=author,
            body=summary,
            ref=repo.default_branch,
            ref_type="branch",
            count=1,
            commit_messages=[title],
            event_id=entry_id,
        )

    if event_type == EventType.CREATE:
        # Tags feed — ref_type="tag"
        return Event(
            event_type=EventType.CREATE,
            source=EventSource.FEED,
            repo=repo.full_name,
            title=title,
            url=link,
            created_at=created_at,
            actor=author,
            ref=title,
            ref_type="tag",
            event_id=entry_id,
        )

    return None


def _user_entry_to_event(
    entry: feedparser.FeedParserDict,
    entry_id: str,
    username: str,
) -> Event | None:
    """Convert a user activity feed entry to a unified Event.

    User feed entry <id> tags encode the event type:
      tag:github.com,2008:PushEvent/12345678
    Titles describe the action:
      alice pushed to main at alice/repo
    """
    title = entry.get("title", "")
    link = entry.get("link", "")
    summary = entry.get("summary", "")
    author = entry.get("author", "") or username

    # Parse published/updated time
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct:
        created_at = datetime.fromtimestamp(mktime(time_struct), tz=timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    # Extract event type from <id> tag
    event_type = None
    match = _ID_EVENT_TYPE_RE.match(entry_id)
    if match:
        event_type = _FEED_EVENT_TYPE_MAP.get(match.group(1))
    if event_type is None:
        log.debug("Unknown user feed event type in id: %s", entry_id)
        event_type = EventType.PUSH  # fallback for generic entries

    # Try to extract repo from link URL (e.g. https://github.com/owner/repo/...)
    repo = _extract_repo_from_url(link)

    return Event(
        event_type=event_type,
        source=EventSource.FEED,
        repo=repo,
        title=title,
        url=link,
        created_at=created_at,
        actor=author,
        body=summary,
        event_id=entry_id,
    )


def _extract_repo_from_url(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL."""
    # Match https://github.com/owner/repo/...
    match = re.match(r"https://github\.com/([^/]+/[^/]+)", url)
    return match.group(1) if match else ""
