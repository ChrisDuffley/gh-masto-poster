"""Configuration loading and validation."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

# Maps config key names to the canonical event config key
_EVENT_KEY_DEFAULTS: dict[str, bool] = {
    "releases": True,
    "commits": True,
    "issues": True,
    "pull_requests": True,
    "stars": False,
    "forks": False,
    "comments": True,
    "reviews": True,
    "discussions": True,
    "wiki": False,
    "tags": True,
    "branches": False,
    "security": True,
    "ci": False,
    "invitations": False,
}

_TRUTHY = {"true", "yes", "1", "on"}
_FALSY = {"false", "no", "0", "off"}


@dataclass
class GitHubConfig:
    token: str
    username: str
    repos: list[str] = field(default_factory=list)  # empty = discover all


@dataclass
class MastodonConfig:
    instance_url: str
    access_token: str
    default_visibility: str = "public"


@dataclass
class DaemonConfig:
    poll_interval: int = 120
    state_file: str = "state.json"
    log_level: str = "INFO"
    dry_run: bool = False


@dataclass
class EventsConfig:
    enabled: dict[str, bool] = field(default_factory=lambda: dict(_EVENT_KEY_DEFAULTS))
    templates: dict[str, str] = field(default_factory=dict)
    visibility: dict[str, str] = field(default_factory=dict)
    content_warning: dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    github: GitHubConfig
    mastodon: MastodonConfig
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    events: EventsConfig = field(default_factory=EventsConfig)


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _parse_list(value: str) -> list[str]:
    """Parse a comma-separated list, stripping whitespace."""
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config(path: str | Path) -> AppConfig:
    """Load configuration from an INI file, with env var overrides for secrets."""
    path = Path(path)
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")

    if not cfg.sections():
        raise ValueError(f"Config file not found or empty: {path}")

    def _get(section: str, key: str, fallback: str = "") -> str:
        return cfg.get(section, key, fallback=fallback).strip()

    # Env vars override file values for secrets
    gh_token = os.environ.get("GITHUB_TOKEN", _get("github", "token"))
    masto_token = os.environ.get("MASTODON_TOKEN", _get("mastodon", "access_token"))

    if not gh_token:
        raise ValueError("GitHub token required: set token under [github] in config or GITHUB_TOKEN env var")
    if not masto_token:
        raise ValueError("Mastodon token required: set access_token under [mastodon] in config or MASTODON_TOKEN env var")

    github = GitHubConfig(
        token=gh_token,
        username=_get("github", "username"),
        repos=_parse_list(_get("github", "repos")),
    )

    mastodon = MastodonConfig(
        instance_url=_get("mastodon", "instance_url").rstrip("/"),
        access_token=masto_token,
        default_visibility=_get("mastodon", "default_visibility") or "public",
    )

    if not mastodon.instance_url:
        raise ValueError("Mastodon instance_url required in [mastodon] config")

    daemon = DaemonConfig(
        poll_interval=int(_get("daemon", "poll_interval") or "120"),
        state_file=_get("daemon", "state_file") or "state.json",
        log_level=_get("daemon", "log_level") or "INFO",
    )

    # Merge event enabled flags with defaults
    enabled = dict(_EVENT_KEY_DEFAULTS)
    if cfg.has_section("events"):
        for key in _EVENT_KEY_DEFAULTS:
            val = _get("events", key)
            if val:
                enabled[key] = _parse_bool(val)

    # Per-event-type templates, visibility, and content warnings
    templates: dict[str, str] = {}
    if cfg.has_section("templates"):
        templates = dict(cfg.items("templates"))

    visibility: dict[str, str] = {}
    if cfg.has_section("visibility"):
        visibility = dict(cfg.items("visibility"))

    content_warning: dict[str, str] = {}
    if cfg.has_section("content_warning"):
        content_warning = dict(cfg.items("content_warning"))

    events = EventsConfig(
        enabled=enabled,
        templates=templates,
        visibility=visibility,
        content_warning=content_warning,
    )

    return AppConfig(github=github, mastodon=mastodon, daemon=daemon, events=events)
