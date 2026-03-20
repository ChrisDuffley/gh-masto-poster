"""Tests for the GitHub API client."""

import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from gh_masto_poster.github.api import GitHubAPI
from gh_masto_poster.models import EventSource, EventType, RepoInfo
from gh_masto_poster.state import State

_SAMPLE_EVENTS = [
    {
        "id": "evt1",
        "type": "ReleaseEvent",
        "actor": {"login": "alice", "id": 1},
        "repo": {"id": 1, "name": "alice/myrepo"},
        "payload": {
            "action": "published",
            "release": {
                "tag_name": "v2.0",
                "name": "Version 2.0",
                "html_url": "https://github.com/alice/myrepo/releases/tag/v2.0",
                "body": "New features",
            },
        },
        "created_at": "2026-03-01T12:00:00Z",
    },
    {
        "id": "evt2",
        "type": "IssuesEvent",
        "actor": {"login": "bob", "id": 2},
        "repo": {"id": 1, "name": "alice/myrepo"},
        "payload": {
            "action": "opened",
            "issue": {
                "title": "Bug report",
                "html_url": "https://github.com/alice/myrepo/issues/1",
                "body": "Something is broken",
            },
        },
        "created_at": "2026-03-01T13:00:00Z",
    },
]

_SAMPLE_REPOS = [
    {
        "name": "myrepo",
        "owner": {"login": "alice"},
        "default_branch": "main",
        "topics": ["python"],
    },
]


@pytest.mark.asyncio
async def test_fetch_repo_events(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    api = GitHubAPI("fake_token")
    repo = RepoInfo(owner="alice", name="myrepo")

    with respx.mock:
        respx.get("https://api.github.com/repos/alice/myrepo/events").respond(
            200,
            json=_SAMPLE_EVENTS,
            headers={"etag": '"xyz"', "x-ratelimit-remaining": "4999", "x-ratelimit-limit": "5000"},
        )

        async with httpx.AsyncClient() as client:
            events = await api.fetch_repo_events(client, repo, state)

    assert len(events) == 2
    assert events[0].event_type == EventType.RELEASE
    assert events[0].source == EventSource.API
    assert events[1].event_type == EventType.ISSUES
    assert events[1].action == "opened"
    assert state.get_api_etag("/repos/alice/myrepo/events") == '"xyz"'
    assert api.rate_remaining == 4999


@pytest.mark.asyncio
async def test_fetch_repo_events_304(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    state.set_api_etag("/repos/alice/myrepo/events", '"xyz"')
    api = GitHubAPI("fake_token")
    repo = RepoInfo(owner="alice", name="myrepo")

    with respx.mock:
        respx.get("https://api.github.com/repos/alice/myrepo/events").respond(304)

        async with httpx.AsyncClient() as client:
            events = await api.fetch_repo_events(client, repo, state)

    assert len(events) == 0


@pytest.mark.asyncio
async def test_discover_repos(tmp_path) -> None:
    api = GitHubAPI("fake_token")

    with respx.mock:
        route = respx.get("https://api.github.com/user/repos")
        route.side_effect = [
            httpx.Response(200, json=_SAMPLE_REPOS, headers={"x-ratelimit-remaining": "4998", "x-ratelimit-limit": "5000"}),
            httpx.Response(200, json=[]),  # empty page signals end
        ]

        async with httpx.AsyncClient() as client:
            repos = await api.discover_repos(client)

    assert len(repos) == 1
    assert repos[0].full_name == "alice/myrepo"
    assert repos[0].default_branch == "main"


_SAMPLE_USER_EVENTS = [
    {
        "id": "user_evt1",
        "type": "PushEvent",
        "actor": {"login": "alice", "id": 1},
        "repo": {"id": 1, "name": "alice/myrepo"},
        "payload": {
            "ref": "refs/heads/main",
            "before": "aaaa000",
            "head": "bbbb111",
            "commits": [
                {"message": "fix typo", "sha": "bbbb111"},
            ],
        },
        "created_at": "2026-03-02T10:00:00Z",
    },
    {
        "id": "user_evt2",
        "type": "IssuesEvent",
        "actor": {"login": "alice", "id": 1},
        "repo": {"id": 2, "name": "alice/webapp"},
        "payload": {
            "action": "opened",
            "issue": {
                "title": "Login broken",
                "html_url": "https://github.com/alice/webapp/issues/99",
                "body": "Cannot log in",
            },
        },
        "created_at": "2026-03-02T11:00:00Z",
    },
]


@pytest.mark.asyncio
async def test_fetch_user_events(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    api = GitHubAPI("fake_token")

    with respx.mock:
        respx.get("https://api.github.com/users/alice/events").respond(
            200,
            json=_SAMPLE_USER_EVENTS,
            headers={
                "etag": '"user_xyz"',
                "x-ratelimit-remaining": "4990",
                "x-ratelimit-limit": "5000",
            },
        )

        async with httpx.AsyncClient() as client:
            events = await api.fetch_user_events(client, "alice", state)

    assert len(events) == 2
    assert events[0].event_type == EventType.PUSH
    assert events[0].repo == "alice/myrepo"
    assert events[1].event_type == EventType.ISSUES
    assert events[1].repo == "alice/webapp"
    assert events[1].action == "opened"
    assert state.get_api_etag("/users/alice/events") == '"user_xyz"'


@pytest.mark.asyncio
async def test_fetch_user_events_304(tmp_path) -> None:
    state = State(tmp_path / "state.json")
    state.set_api_etag("/users/alice/events", '"user_xyz"')
    api = GitHubAPI("fake_token")

    with respx.mock:
        respx.get("https://api.github.com/users/alice/events").respond(304)

        async with httpx.AsyncClient() as client:
            events = await api.fetch_user_events(client, "alice", state)

    assert len(events) == 0


@pytest.mark.asyncio
async def test_rate_low() -> None:
    api = GitHubAPI("fake_token")
    api.rate_remaining = 400
    api.rate_limit = 5000
    assert api.rate_low  # 400/5000 = 8% < 10%

    api.rate_remaining = 600
    assert not api.rate_low  # 600/5000 = 12% > 10%


@pytest.mark.asyncio
async def test_seconds_until_reset() -> None:
    api = GitHubAPI("fake_token")
    api.rate_reset = int(time.time()) + 120
    secs = api.seconds_until_reset()
    assert 118 <= secs <= 121

    api.rate_reset = 0
    assert api.seconds_until_reset() == 0.0
