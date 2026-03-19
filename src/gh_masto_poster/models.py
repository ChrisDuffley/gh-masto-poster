"""Unified data models for gh-masto-poster."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class EventType(str, Enum):
    """All supported GitHub event types."""

    RELEASE = "ReleaseEvent"
    PUSH = "PushEvent"
    CREATE = "CreateEvent"
    DELETE = "DeleteEvent"
    ISSUES = "IssuesEvent"
    ISSUE_COMMENT = "IssueCommentEvent"
    PULL_REQUEST = "PullRequestEvent"
    PULL_REQUEST_REVIEW = "PullRequestReviewEvent"
    PULL_REQUEST_REVIEW_COMMENT = "PullRequestReviewCommentEvent"
    WATCH = "WatchEvent"
    FORK = "ForkEvent"
    COMMIT_COMMENT = "CommitCommentEvent"
    GOLLUM = "GollumEvent"
    MEMBER = "MemberEvent"
    PUBLIC = "PublicEvent"
    DISCUSSION = "DiscussionEvent"
    SECURITY_ADVISORY = "SecurityAdvisory"
    CHECK_SUITE = "CheckSuiteNotification"
    DEPENDABOT_ALERT = "DependabotAlert"
    REPOSITORY_INVITATION = "RepositoryInvitation"


class EventSource(str, Enum):
    """Where the event was discovered."""

    FEED = "feed"
    API = "api"
    NOTIFICATION = "notification"


@dataclass
class RepoInfo:
    """Minimal repository information."""

    owner: str
    name: str
    default_branch: str = "main"
    topics: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class Event:
    """Unified event representation from either feeds or the API."""

    event_type: EventType
    source: EventSource
    repo: str  # "owner/name"
    title: str
    url: str
    created_at: datetime
    actor: str = ""
    action: str = ""  # opened, closed, merged, published, etc.
    body: str = ""
    ref: str = ""  # branch or tag name
    ref_type: str = ""  # "branch", "tag", "repo"
    count: int = 0  # commit count for PushEvent
    commit_messages: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # Unique identifier — GitHub event ID or feed entry hash
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            # Generate a deterministic ID from key fields
            raw = f"{self.event_type.value}:{self.repo}:{self.url}:{self.created_at.isoformat()}"
            self.event_id = hashlib.sha256(raw.encode()).hexdigest()[:16]

        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)

    def to_template_vars(self) -> dict:
        """Return a dict of all variables available for template rendering."""
        return {
            "event_type": self.event_type.value,
            "repo": self.repo,
            "title": self.title,
            "url": self.url,
            "actor": self.actor,
            "action": self.action,
            "body": self.body,
            "body_truncated": _truncate(self.body, 200),
            "ref": self.ref,
            "ref_type": self.ref_type,
            "count": self.count,
            "commit_messages": "\n".join(f"• {m}" for m in self.commit_messages[:5]),
            "created_at": self.created_at.isoformat(),
            # Convenience aliases
            "tag": self.ref if self.ref_type == "tag" else "",
            "branch": self.ref if self.ref_type == "branch" else "",
            "issue_title": self.title,
            "pr_title": self.title,
            "page_title": self.extra.get("page_title", ""),
            "member": self.extra.get("member", ""),
        }


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
