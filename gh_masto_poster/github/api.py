"""GitHub REST API client — events, repo discovery, notifications."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from gh_masto_poster.models import Event, EventSource, EventType, RepoInfo
from gh_masto_poster.state import State

log = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"

# Map GitHub event type strings to our EventType enum
_EVENT_TYPE_MAP: dict[str, EventType] = {
    "ReleaseEvent": EventType.RELEASE,
    "PushEvent": EventType.PUSH,
    "CreateEvent": EventType.CREATE,
    "DeleteEvent": EventType.DELETE,
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
    "DiscussionEvent": EventType.DISCUSSION,
}

# Notification subject types to our EventType
_NOTIFICATION_TYPE_MAP: dict[str, EventType] = {
    "Issue": EventType.ISSUES,
    "PullRequest": EventType.PULL_REQUEST,
    "Commit": EventType.PUSH,
    "Discussion": EventType.DISCUSSION,
    "SecurityAdvisory": EventType.SECURITY_ADVISORY,
    "Release": EventType.RELEASE,
    "CheckSuite": EventType.CHECK_SUITE,
    "RepositoryVulnerabilityAlert": EventType.SECURITY_ADVISORY,
    "RepositoryDependabotAlertsThread": EventType.DEPENDABOT_ALERT,
    "RepositoryInvitation": EventType.REPOSITORY_INVITATION,
}


class GitHubAPI:
    """Async GitHub REST API client with rate-limit awareness."""

    def __init__(self, token: str) -> None:
        self._token = token
        self.rate_remaining: int = 5000
        self.rate_limit: int = 5000
        self.rate_reset: int = 0
        self.poll_interval: int = 60  # seconds, from X-Poll-Interval

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _update_rate_info(self, resp: httpx.Response) -> None:
        """Extract rate-limit info from response headers."""
        if "x-ratelimit-remaining" in resp.headers:
            self.rate_remaining = int(resp.headers["x-ratelimit-remaining"])
        if "x-ratelimit-limit" in resp.headers:
            self.rate_limit = int(resp.headers["x-ratelimit-limit"])
        if "x-ratelimit-reset" in resp.headers:
            self.rate_reset = int(resp.headers["x-ratelimit-reset"])
        if "x-poll-interval" in resp.headers:
            self.poll_interval = int(resp.headers["x-poll-interval"])

    @property
    def rate_low(self) -> bool:
        """True if less than 10% of rate limit remains."""
        return self.rate_remaining < self.rate_limit * 0.1

    # ── Repo discovery ──────────────────────────────────────────

    async def discover_repos(self, client: httpx.AsyncClient) -> list[RepoInfo]:
        """Fetch all repos for the authenticated user."""
        repos: list[RepoInfo] = []
        page = 1
        while True:
            resp = await client.get(
                f"{_API_BASE}/user/repos",
                headers=self._headers(),
                params={"per_page": 100, "page": page, "sort": "updated"},
            )
            self._update_rate_info(resp)
            if resp.status_code != 200:
                log.error("Failed to fetch repos (page %d): %d", page, resp.status_code)
                break

            data = resp.json()
            if not data:
                break

            for r in data:
                repos.append(RepoInfo(
                    owner=r["owner"]["login"],
                    name=r["name"],
                    default_branch=r.get("default_branch", "main"),
                    topics=r.get("topics", []),
                ))
            page += 1

        log.info("Discovered %d repos", len(repos))
        return repos

    # ── Repo events ─────────────────────────────────────────────

    async def fetch_repo_events(
        self,
        client: httpx.AsyncClient,
        repo: RepoInfo,
        state: State,
    ) -> list[Event]:
        """Fetch events for a single repo via the Events API."""
        endpoint = f"/repos/{repo.full_name}/events"
        url = f"{_API_BASE}{endpoint}"

        headers = self._headers()
        etag = state.get_api_etag(endpoint)
        if etag:
            headers["If-None-Match"] = etag

        resp = await client.get(url, headers=headers, params={"per_page": 100})
        self._update_rate_info(resp)

        if resp.status_code == 304:
            log.debug("No new repo events (304): %s", repo.full_name)
            return []

        if resp.status_code != 200:
            log.warning("Repo events fetch failed (%d): %s", resp.status_code, repo.full_name)
            return []

        new_etag = resp.headers.get("etag")
        if new_etag:
            state.set_api_etag(endpoint, new_etag)

        events: list[Event] = []
        for raw in resp.json():
            gh_id = raw.get("id", "")
            if state.has_event(gh_id):
                continue
            event = _api_event_to_event(raw, gh_id)
            if event:
                events.append(event)

        log.info("Repo events %s: %d new", repo.full_name, len(events))
        return events

    # ── Notifications ───────────────────────────────────────────

    async def fetch_notifications(
        self,
        client: httpx.AsyncClient,
        state: State,
    ) -> list[Event]:
        """Fetch unread notifications and convert to Events."""
        endpoint = "/notifications"
        url = f"{_API_BASE}{endpoint}"

        headers = self._headers()
        etag = state.get_api_etag(endpoint)
        if etag:
            headers["If-None-Match"] = etag

        resp = await client.get(url, headers=headers, params={"all": "false"})
        self._update_rate_info(resp)

        if resp.status_code == 304:
            log.debug("No new notifications (304)")
            return []
        if resp.status_code != 200:
            log.warning("Notifications fetch failed (%d)", resp.status_code)
            return []

        new_etag = resp.headers.get("etag")
        if new_etag:
            state.set_api_etag(endpoint, new_etag)

        events: list[Event] = []
        for notif in resp.json():
            notif_id = f"notif:{notif['id']}"
            if state.has_event(notif_id):
                continue
            event = _notification_to_event(notif, notif_id)
            if event:
                events.append(event)

        log.info("Notifications: %d new", len(events))
        return events


# ── Event parsing helpers ────────────────────────────────────────


def _api_event_to_event(raw: dict, gh_id: str) -> Event | None:
    """Convert a raw GitHub API event to our unified Event model."""
    type_str = raw.get("type", "")
    event_type = _EVENT_TYPE_MAP.get(type_str)
    if event_type is None:
        log.debug("Skipping unknown event type: %s", type_str)
        return None

    repo_name = raw.get("repo", {}).get("name", "")
    actor = raw.get("actor", {}).get("login", "")
    payload = raw.get("payload", {})
    created_at = _parse_iso(raw.get("created_at", ""))

    if event_type == EventType.PUSH:
        commits = payload.get("commits", [])
        ref = payload.get("ref", "").removeprefix("refs/heads/")
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"{len(commits)} commit(s) to {ref}",
            url=f"https://github.com/{repo_name}/compare/{payload.get('before', '')[:7]}...{payload.get('head', '')[:7]}",
            created_at=created_at,
            actor=actor,
            ref=ref,
            ref_type="branch",
            count=len(commits),
            commit_messages=[c.get("message", "").split("\n")[0] for c in commits[:5]],
            event_id=gh_id,
        )

    if event_type == EventType.RELEASE:
        release = payload.get("release", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=release.get("name", "") or release.get("tag_name", ""),
            url=release.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action=payload.get("action", "published"),
            body=release.get("body", ""),
            ref=release.get("tag_name", ""),
            ref_type="tag",
            event_id=gh_id,
        )

    if event_type == EventType.CREATE:
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"New {payload.get('ref_type', '')} {payload.get('ref', '')}",
            url=f"https://github.com/{repo_name}",
            created_at=created_at,
            actor=actor,
            ref=payload.get("ref", ""),
            ref_type=payload.get("ref_type", ""),
            event_id=gh_id,
        )

    if event_type == EventType.DELETE:
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"Deleted {payload.get('ref_type', '')} {payload.get('ref', '')}",
            url=f"https://github.com/{repo_name}",
            created_at=created_at,
            actor=actor,
            ref=payload.get("ref", ""),
            ref_type=payload.get("ref_type", ""),
            event_id=gh_id,
        )

    if event_type == EventType.ISSUES:
        issue = payload.get("issue", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=issue.get("title", ""),
            url=issue.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action=payload.get("action", ""),
            body=issue.get("body", "") or "",
            event_id=gh_id,
        )

    if event_type == EventType.ISSUE_COMMENT:
        issue = payload.get("issue", {})
        comment = payload.get("comment", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=issue.get("title", ""),
            url=comment.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action="commented",
            body=comment.get("body", "") or "",
            event_id=gh_id,
        )

    if event_type == EventType.PULL_REQUEST:
        pr = payload.get("pull_request", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=pr.get("title", ""),
            url=pr.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action=payload.get("action", ""),
            body=pr.get("body", "") or "",
            event_id=gh_id,
        )

    if event_type == EventType.PULL_REQUEST_REVIEW:
        pr = payload.get("pull_request", {})
        review = payload.get("review", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=pr.get("title", ""),
            url=review.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action=review.get("state", ""),
            body=review.get("body", "") or "",
            event_id=gh_id,
        )

    if event_type == EventType.PULL_REQUEST_REVIEW_COMMENT:
        pr = payload.get("pull_request", {})
        comment = payload.get("comment", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=pr.get("title", ""),
            url=comment.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action="commented",
            body=comment.get("body", "") or "",
            event_id=gh_id,
        )

    if event_type == EventType.WATCH:
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"{actor} starred {repo_name}",
            url=f"https://github.com/{repo_name}",
            created_at=created_at,
            actor=actor,
            action="starred",
            event_id=gh_id,
        )

    if event_type == EventType.FORK:
        forkee = payload.get("forkee", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"{actor} forked {repo_name}",
            url=forkee.get("html_url", f"https://github.com/{repo_name}"),
            created_at=created_at,
            actor=actor,
            action="forked",
            event_id=gh_id,
        )

    if event_type == EventType.COMMIT_COMMENT:
        comment = payload.get("comment", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"Comment on commit in {repo_name}",
            url=comment.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action="commented",
            body=comment.get("body", "") or "",
            event_id=gh_id,
        )

    if event_type == EventType.GOLLUM:
        pages = payload.get("pages", [])
        page_title = pages[0].get("title", "") if pages else ""
        page_url = pages[0].get("html_url", "") if pages else ""
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"Wiki: {page_title}",
            url=page_url,
            created_at=created_at,
            actor=actor,
            action=pages[0].get("action", "") if pages else "",
            extra={"page_title": page_title},
            event_id=gh_id,
        )

    if event_type == EventType.MEMBER:
        member = payload.get("member", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"{member.get('login', '')} added to {repo_name}",
            url=f"https://github.com/{repo_name}",
            created_at=created_at,
            actor=actor,
            action="added",
            extra={"member": member.get("login", "")},
            event_id=gh_id,
        )

    if event_type == EventType.PUBLIC:
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=f"{repo_name} is now public",
            url=f"https://github.com/{repo_name}",
            created_at=created_at,
            actor=actor,
            action="publicized",
            event_id=gh_id,
        )

    if event_type == EventType.DISCUSSION:
        discussion = payload.get("discussion", {})
        return Event(
            event_type=event_type,
            source=EventSource.API,
            repo=repo_name,
            title=discussion.get("title", ""),
            url=discussion.get("html_url", ""),
            created_at=created_at,
            actor=actor,
            action=payload.get("action", "created"),
            body=discussion.get("body", "") or "",
            event_id=gh_id,
        )

    return None


def _notification_to_event(notif: dict, notif_id: str) -> Event | None:
    """Convert a GitHub notification to a unified Event."""
    subject = notif.get("subject", {})
    subject_type = subject.get("type", "")
    event_type = _NOTIFICATION_TYPE_MAP.get(subject_type)

    repo = notif.get("repository", {})
    repo_name = repo.get("full_name", "")
    title = subject.get("title", "")
    reason = notif.get("reason", "")
    updated = _parse_iso(notif.get("updated_at", ""))

    # Build a web URL from the API URL
    url = _api_url_to_web_url(subject.get("url", ""), repo_name)

    if event_type == EventType.SECURITY_ADVISORY or reason == "security_alert":
        return Event(
            event_type=EventType.SECURITY_ADVISORY,
            source=EventSource.NOTIFICATION,
            repo=repo_name,
            title=title,
            url=url,
            created_at=updated,
            action="alert",
            extra={"reason": reason},
            event_id=notif_id,
        )

    if event_type is None:
        log.debug("Skipping notification type: %s", subject_type)
        return None

    return Event(
        event_type=event_type,
        source=EventSource.NOTIFICATION,
        repo=repo_name,
        title=title,
        url=url,
        created_at=updated,
        action=reason,
        extra={"reason": reason},
        event_id=notif_id,
    )


def _parse_iso(s: str) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _api_url_to_web_url(api_url: str, repo_name: str) -> str:
    """Best-effort conversion of a GitHub API URL to a web URL."""
    if not api_url:
        return f"https://github.com/{repo_name}" if repo_name else ""
    # e.g. https://api.github.com/repos/owner/repo/issues/123
    return (
        api_url
        .replace("https://api.github.com/repos/", "https://github.com/")
        .replace("/pulls/", "/pull/")
    )
