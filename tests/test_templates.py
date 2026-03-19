"""Tests for the template renderer."""

from datetime import datetime, timezone

from gh_masto_poster.models import Event, EventSource, EventType
from gh_masto_poster.templates import TemplateRenderer


def _make_event(**kwargs) -> Event:
    defaults = {
        "event_type": EventType.RELEASE,
        "source": EventSource.FEED,
        "repo": "alice/myrepo",
        "title": "v1.0",
        "url": "https://github.com/alice/myrepo/releases/tag/v1.0",
        "created_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return Event(**defaults)


def test_default_release_template() -> None:
    renderer = TemplateRenderer()
    event = _make_event(
        ref="v1.0",
        ref_type="tag",
        body="Bug fixes and improvements",
    )
    text = renderer.render(event)
    assert "New release" in text
    assert "v1.0" in text
    assert "alice/myrepo" in text
    assert "https://github.com/alice/myrepo/releases/tag/v1.0" in text


def test_custom_template() -> None:
    renderer = TemplateRenderer(
        custom_templates={"releases": "NEW: {{ title }} ({{ repo }})"},
    )
    event = _make_event()
    text = renderer.render(event)
    assert text == "NEW: v1.0 (alice/myrepo)"


def test_push_template() -> None:
    renderer = TemplateRenderer()
    event = _make_event(
        event_type=EventType.PUSH,
        title="3 commits",
        ref="main",
        ref_type="branch",
        count=3,
        commit_messages=["fix bug", "add feature", "update docs"],
    )
    text = renderer.render(event)
    assert "commit" in text
    assert "3" in text
    assert "main" in text


def test_truncation() -> None:
    renderer = TemplateRenderer(character_limit=50)
    event = _make_event(
        body="A" * 1000,
    )
    text = renderer.render(event)
    # Should be truncated but still contain URL
    assert len(text) <= 200  # generous bound (URL is 23 chars in Mastodon)
    assert "…" in text


def test_issue_template() -> None:
    renderer = TemplateRenderer()
    event = _make_event(
        event_type=EventType.ISSUES,
        title="Bug in login",
        action="opened",
        url="https://github.com/alice/myrepo/issues/42",
    )
    text = renderer.render(event)
    assert "Issue" in text
    assert "opened" in text
    assert "Bug in login" in text


def test_star_template() -> None:
    renderer = TemplateRenderer()
    event = _make_event(
        event_type=EventType.WATCH,
        actor="bob",
        title="bob starred alice/myrepo",
    )
    text = renderer.render(event)
    assert "starred" in text
    assert "bob" in text
