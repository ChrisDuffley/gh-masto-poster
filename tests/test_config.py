"""Tests for the config module."""

import os
from pathlib import Path

import pytest

from gh_masto_poster.config import load_config


_MINIMAL_CONFIG = """\
[github]
token = ghp_test123
username = testuser

[mastodon]
instance_url = https://mastodon.example
access_token = masto_token_abc
"""

_FULL_CONFIG = """\
[github]
token = ghp_full
username = fulluser
repos = owner/repo1, owner/repo2

[mastodon]
instance_url = https://mastodon.example/
access_token = masto_full
default_visibility = unlisted

[daemon]
poll_interval = 60
state_file = custom_state.json
log_level = DEBUG

[events]
releases = true
commits = false
stars = true
forks = true
wiki = true
ci = true
invitations = true

[templates]
releases = Release: {{ title }}

[visibility]
security = direct

[content_warning]
security = Security Issue
"""


def test_minimal_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(_MINIMAL_CONFIG)

    config = load_config(cfg_path)
    assert config.github.token == "ghp_test123"
    assert config.github.username == "testuser"
    assert config.mastodon.instance_url == "https://mastodon.example"
    assert config.mastodon.default_visibility == "public"
    assert config.daemon.poll_interval == 120
    assert config.events.enabled["releases"] is True
    assert config.events.enabled["stars"] is False
    assert config.events.enabled["ci"] is False
    assert config.events.enabled["invitations"] is False


def test_full_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(_FULL_CONFIG)

    config = load_config(cfg_path)
    assert config.github.repos == ["owner/repo1", "owner/repo2"]
    assert config.mastodon.instance_url == "https://mastodon.example"  # trailing slash stripped
    assert config.daemon.poll_interval == 60
    assert config.daemon.state_file == "custom_state.json"
    assert config.events.enabled["commits"] is False
    assert config.events.enabled["stars"] is True
    assert config.events.enabled["ci"] is True
    assert config.events.enabled["invitations"] is True
    assert config.events.enabled["stars"] is True
    assert config.events.templates["releases"] == "Release: {{ title }}"
    assert config.events.visibility["security"] == "direct"
    assert config.events.content_warning["security"] == "Security Issue"


def test_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(_MINIMAL_CONFIG)

    monkeypatch.setenv("GITHUB_TOKEN", "env_gh_token")
    monkeypatch.setenv("MASTODON_TOKEN", "env_masto_token")

    config = load_config(cfg_path)
    assert config.github.token == "env_gh_token"
    assert config.mastodon.access_token == "env_masto_token"


def test_missing_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("""\
[github]
username = user
[mastodon]
instance_url = https://mastodon.example
""")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("MASTODON_TOKEN", raising=False)

    with pytest.raises(ValueError, match="GitHub token required"):
        load_config(cfg_path)


def test_missing_instance_url(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("""\
[github]
token = ghp_x
[mastodon]
access_token = masto_x
""")

    with pytest.raises(ValueError, match="instance_url required"):
        load_config(cfg_path)
