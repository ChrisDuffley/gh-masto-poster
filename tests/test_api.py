"""Tests for the GitHub API client."""

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


@pytest.mark.asyncio
async def test_rate_low() -> None:
    api = GitHubAPI("fake_token")
    api.rate_remaining = 400
    api.rate_limit = 5000
    assert api.rate_low  # 400/5000 = 8% < 10%

    api.rate_remaining = 600
    assert not api.rate_low  # 600/5000 = 12% > 10%
