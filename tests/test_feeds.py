"""Tests for the GitHub feed fetcher."""

from datetime import datetime, timezone

import httpx
import pytest
import respx

from gh_masto_poster.github.feeds import fetch_feed_events, fetch_user_feed_events
from gh_masto_poster.models import EventSource, EventType, RepoInfo
from gh_masto_poster.state import State

_SAMPLE_ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release notes from myrepo</title>
  <entry>
    <id>tag:github.com,2008:Repository/123/v1.0.0</id>
    <title>v1.0.0</title>
    <link rel="alternate" type="text/html" href="https://github.com/alice/myrepo/releases/tag/v1.0.0"/>
    <author><name>alice</name></author>
    <updated>2026-03-01T12:00:00Z</updated>
    <summary>Initial release</summary>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/123/v0.9.0</id>
    <title>v0.9.0</title>
    <link rel="alternate" type="text/html" href="https://github.com/alice/myrepo/releases/tag/v0.9.0"/>
    <author><name>alice</name></author>
    <updated>2026-02-01T12:00:00Z</updated>
    <summary>Beta release</summary>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_fetch_releases(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    repo = RepoInfo(owner="alice", name="myrepo")

    with respx.mock:
        respx.get("https://github.com/alice/myrepo/releases.atom").respond(
            200, text=_SAMPLE_ATOM, headers={"etag": '"abc"'}
        )
        # Disable commits and tags for this test
        async with httpx.AsyncClient() as client:
            events = await fetch_feed_events(
                client, repo, state,
                releases=True, commits=False, tags=False,
            )

    assert len(events) == 2
    assert events[0].event_type == EventType.RELEASE
    assert events[0].source == EventSource.FEED
    assert "v1.0.0" in events[0].title
    assert state.get_feed_etag("https://github.com/alice/myrepo/releases.atom") == '"abc"'


@pytest.mark.asyncio
async def test_etag_304(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    state.set_feed_etag("https://github.com/alice/myrepo/releases.atom", '"abc"')
    repo = RepoInfo(owner="alice", name="myrepo")

    with respx.mock:
        respx.get("https://github.com/alice/myrepo/releases.atom").respond(304)

        async with httpx.AsyncClient() as client:
            events = await fetch_feed_events(
                client, repo, state,
                releases=True, commits=False, tags=False,
            )

    assert len(events) == 0


@pytest.mark.asyncio
async def test_dedup_with_state(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    state.record_event("tag:github.com,2008:Repository/123/v1.0.0")
    repo = RepoInfo(owner="alice", name="myrepo")

    with respx.mock:
        respx.get("https://github.com/alice/myrepo/releases.atom").respond(
            200, text=_SAMPLE_ATOM,
        )

        async with httpx.AsyncClient() as client:
            events = await fetch_feed_events(
                client, repo, state,
                releases=True, commits=False, tags=False,
            )

    # Only v0.9.0 should be new
    assert len(events) == 1
    assert "v0.9.0" in events[0].title


_SAMPLE_USER_ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>alice's Activity</title>
  <entry>
    <id>tag:github.com,2008:PushEvent/40000001</id>
    <title>alice pushed to main at alice/myrepo</title>
    <link rel="alternate" type="text/html" href="https://github.com/alice/myrepo/compare/abc1234...def5678"/>
    <author><name>alice</name></author>
    <updated>2026-03-01T14:00:00Z</updated>
    <content type="html">1 commit pushed</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:IssuesEvent/40000002</id>
    <title>alice opened issue alice/webapp#42</title>
    <link rel="alternate" type="text/html" href="https://github.com/alice/webapp/issues/42"/>
    <author><name>alice</name></author>
    <updated>2026-03-01T13:00:00Z</updated>
    <content type="html">Bug report</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:CreateEvent/40000003</id>
    <title>alice created branch feature-x at alice/myrepo</title>
    <link rel="alternate" type="text/html" href="https://github.com/alice/myrepo/tree/feature-x"/>
    <author><name>alice</name></author>
    <updated>2026-03-01T12:00:00Z</updated>
    <content type="html">New branch</content>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_fetch_user_feed(tmp_path) -> None:
    state = State(tmp_path / "state.json")

    with respx.mock:
        respx.get("https://github.com/alice.atom").respond(
            200, text=_SAMPLE_USER_ATOM, headers={"etag": '"user123"'}
        )

        async with httpx.AsyncClient() as client:
            events = await fetch_user_feed_events(client, "alice", state)

    assert len(events) == 3
    # Check event types parsed from <id> tags
    assert events[0].event_type == EventType.PUSH
    assert events[1].event_type == EventType.ISSUES
    assert events[2].event_type == EventType.CREATE
    # Check repo extracted from URL
    assert events[0].repo == "alice/myrepo"
    assert events[1].repo == "alice/webapp"
    assert events[0].source == EventSource.FEED
    assert events[0].actor == "alice"
    # Check ETag saved
    assert state.get_feed_etag("https://github.com/alice.atom") == '"user123"'


@pytest.mark.asyncio
async def test_user_feed_dedup(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    state.record_event("tag:github.com,2008:PushEvent/40000001")

    with respx.mock:
        respx.get("https://github.com/alice.atom").respond(
            200, text=_SAMPLE_USER_ATOM,
        )

        async with httpx.AsyncClient() as client:
            events = await fetch_user_feed_events(client, "alice", state)

    # PushEvent already seen, only 2 new
    assert len(events) == 2


@pytest.mark.asyncio
async def test_user_feed_304(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    state.set_feed_etag("https://github.com/alice.atom", '"user123"')

    with respx.mock:
        respx.get("https://github.com/alice.atom").respond(304)

        async with httpx.AsyncClient() as client:
            events = await fetch_user_feed_events(client, "alice", state)

    assert len(events) == 0
