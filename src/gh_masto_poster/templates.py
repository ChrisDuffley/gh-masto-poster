"""Template engine — render Events into Mastodon post text."""

from __future__ import annotations

import logging

from jinja2 import BaseLoader, Environment

from gh_masto_poster.models import Event, EventType

log = logging.getLogger(__name__)

# Default templates per event type (Jinja2 syntax)
DEFAULT_TEMPLATES: dict[str, str] = {
    EventType.RELEASE.value: "New release {{ ref }} of {{ repo }}: {{ title }}\n\n{{ body_truncated }}\n\n{{ url }}",
    EventType.PUSH.value: "{{ count }} new commit(s) to {{ repo }}/{{ branch }}\n\n{{ commit_messages }}\n\n{{ url }}",
    EventType.CREATE.value: "{% if ref_type == 'tag' %}New tag {{ ref }} in {{ repo }}{% else %}New branch {{ ref }} in {{ repo }}{% endif %}\n\n{{ url }}",
    EventType.DELETE.value: "Deleted {{ ref_type }} {{ ref }} in {{ repo }}",
    EventType.ISSUES.value: "Issue {{ action }}: {{ title }} in {{ repo }}\n\n{{ url }}",
    EventType.ISSUE_COMMENT.value: "Comment on {{ issue_title }} in {{ repo }}\n\n{{ url }}",
    EventType.PULL_REQUEST.value: "PR {{ action }}: {{ title }} in {{ repo }}\n\n{{ url }}",
    EventType.PULL_REQUEST_REVIEW.value: "PR review on {{ pr_title }} in {{ repo }}\n\n{{ url }}",
    EventType.PULL_REQUEST_REVIEW_COMMENT.value: "Review comment on {{ pr_title }} in {{ repo }}\n\n{{ url }}",
    EventType.WATCH.value: "{{ actor }} starred {{ repo }}",
    EventType.FORK.value: "{{ actor }} forked {{ repo }}\n\n{{ url }}",
    EventType.COMMIT_COMMENT.value: "Comment on commit in {{ repo }}\n\n{{ url }}",
    EventType.GOLLUM.value: "Wiki updated in {{ repo }}: {{ page_title }}\n\n{{ url }}",
    EventType.MEMBER.value: "{{ member }} added to {{ repo }}",
    EventType.PUBLIC.value: "{{ repo }} is now public!",
    EventType.DISCUSSION.value: "Discussion: {{ title }} in {{ repo }}\n\n{{ url }}",
    EventType.SECURITY_ADVISORY.value: "Security alert in {{ repo }}: {{ title }}\n\n{{ url }}",
    EventType.CHECK_SUITE.value: "CI run in {{ repo }}: {{ title }}\n\n{{ url }}",
    EventType.DEPENDABOT_ALERT.value: "Dependabot alert in {{ repo }}: {{ title }}\n\n{{ url }}",
    EventType.REPOSITORY_INVITATION.value: "Repository invitation: {{ title }}\n\n{{ url }}",
}


class TemplateRenderer:
    """Renders Events into Mastodon post text using Jinja2 templates."""

    def __init__(
        self,
        custom_templates: dict[str, str] | None = None,
        character_limit: int = 500,
    ) -> None:
        self._env = Environment(loader=BaseLoader(), autoescape=False)
        self._character_limit = character_limit

        # Build final template map: custom overrides defaults
        self._templates: dict[str, str] = dict(DEFAULT_TEMPLATES)
        if custom_templates:
            # Custom templates use config keys (e.g. "releases") not event type values
            key_to_type = _config_key_to_event_type()
            for key, tmpl in custom_templates.items():
                event_type_value = key_to_type.get(key)
                if event_type_value:
                    self._templates[event_type_value] = tmpl
                else:
                    log.warning("Unknown template key: %s", key)

    def render(self, event: Event) -> str:
        """Render an event to post text, truncated to fit the character limit."""
        template_str = self._templates.get(event.event_type.value)
        if not template_str:
            log.warning("No template for event type: %s", event.event_type.value)
            return ""

        template = self._env.from_string(template_str)
        text = template.render(**event.to_template_vars())

        # Clean up excessive whitespace
        lines = text.split("\n")
        text = "\n".join(line for line in lines if line or lines[lines.index(line) - 1] != "" if lines.index(line) > 0 or line)
        text = text.strip()

        # Truncate to character limit, preserving the URL
        if len(text) > self._character_limit:
            text = _truncate_with_url(text, event.url, self._character_limit)

        return text


def _truncate_with_url(text: str, url: str, limit: int) -> str:
    """Truncate text to fit within limit, keeping the URL at the end."""
    if not url:
        return text[: limit - 1] + "…"

    # URLs count as 23 chars on Mastodon regardless of length
    url_display_len = 23
    suffix = f"\n\n{url}"
    available = limit - url_display_len - 3  # 3 for "\n\n" + "…"

    # Remove existing URL from text to avoid duplication
    text_without_url = text.replace(url, "").strip()

    if len(text_without_url) <= available:
        return text_without_url + suffix

    return text_without_url[:available] + "…" + suffix


def _config_key_to_event_type() -> dict[str, str]:
    """Map config key names to EventType values."""
    return {
        "releases": EventType.RELEASE.value,
        "commits": EventType.PUSH.value,
        "tags": EventType.CREATE.value,
        "issues": EventType.ISSUES.value,
        "comments": EventType.ISSUE_COMMENT.value,
        "pull_requests": EventType.PULL_REQUEST.value,
        "reviews": EventType.PULL_REQUEST_REVIEW.value,
        "stars": EventType.WATCH.value,
        "forks": EventType.FORK.value,
        "wiki": EventType.GOLLUM.value,
        "discussions": EventType.DISCUSSION.value,
        "security": EventType.SECURITY_ADVISORY.value,
        "ci": EventType.CHECK_SUITE.value,
        "invitations": EventType.REPOSITORY_INVITATION.value,
    }
