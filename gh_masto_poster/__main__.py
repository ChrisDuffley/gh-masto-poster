"""Entry point — daemon main loop, signal handling, CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import httpx

from gh_masto_poster.config import AppConfig, load_config
from gh_masto_poster.github.api import GitHubAPI
from gh_masto_poster.github.events import merge_and_filter
from gh_masto_poster.github.feeds import fetch_feed_events
from gh_masto_poster.mastodon.poster import MastodonPoster
from gh_masto_poster.models import RepoInfo
from gh_masto_poster.state import State
from gh_masto_poster.templates import TemplateRenderer

log = logging.getLogger("gh_masto_poster")

# How many poll cycles between repo-list refreshes
_REPO_REFRESH_INTERVAL = 50

# Throttle delay between Mastodon posts (seconds)
_POST_THROTTLE = 10


async def run(config: AppConfig) -> None:
    """Main async loop: poll → merge → render → post → sleep."""
    state = State(config.daemon.state_file)
    gh_api = GitHubAPI(config.github.token)
    masto = MastodonPoster(
        config.mastodon.instance_url,
        config.mastodon.access_token,
        config.mastodon.default_visibility,
    )

    shutdown = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    async with httpx.AsyncClient(timeout=30) as client:
        # Auto-detect Mastodon character limit from the instance
        char_limit = await masto.detect_character_limit(client)
        renderer = TemplateRenderer(
            custom_templates=config.events.templates,
            character_limit=char_limit,
        )

        repos: list[RepoInfo] = []
        poll_count = 0

        log.info("Starting gh-masto-poster daemon (poll every %ds)", config.daemon.poll_interval)

        while not shutdown.is_set():
            try:
                # Refresh repo list periodically
                if not repos or poll_count % _REPO_REFRESH_INTERVAL == 0:
                    repos = await _discover_repos(client, gh_api, config)

                poll_count += 1
                await _poll_cycle(client, config, gh_api, masto, renderer, state, repos)

            except Exception:
                log.exception("Error in poll cycle")

            # Sleep with shutdown check
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=config.daemon.poll_interval)
            except asyncio.TimeoutError:
                pass

    state.save()
    log.info("Daemon stopped")


async def _discover_repos(
    client: httpx.AsyncClient,
    gh_api: GitHubAPI,
    config: AppConfig,
) -> list[RepoInfo]:
    """Get the list of repos to monitor."""
    if config.github.repos:
        # User specified explicit repos
        repos = []
        for r in config.github.repos:
            parts = r.split("/", 1)
            if len(parts) == 2:
                repos.append(RepoInfo(owner=parts[0], name=parts[1]))
        log.info("Using %d configured repos", len(repos))
        return repos

    # Discover all repos via API
    return await gh_api.discover_repos(client)


async def _poll_cycle(
    client: httpx.AsyncClient,
    config: AppConfig,
    gh_api: GitHubAPI,
    masto: MastodonPoster,
    renderer: TemplateRenderer,
    state: State,
    repos: list[RepoInfo],
) -> None:
    """Run one full poll cycle: fetch → merge → render → post."""
    all_feed_events = []
    all_api_events = []

    for repo in repos:
        # 1. Feed-first: fetch Atom feeds (no auth, lightweight)
        feed_events = await fetch_feed_events(
            client, repo, state,
            releases=config.events.enabled.get("releases", True),
            commits=config.events.enabled.get("commits", True),
            tags=config.events.enabled.get("tags", True),
        )
        all_feed_events.extend(feed_events)

        # 2. API fallback: fetch events for types feeds can't cover
        if not gh_api.rate_low:
            api_events = await gh_api.fetch_repo_events(client, repo, state)
            all_api_events.extend(api_events)
        else:
            log.warning("Skipping API events for %s (rate limit low)", repo.full_name)

    # 3. Notifications (global, not per-repo)
    notification_events = []
    if not gh_api.rate_low:
        notification_events = await gh_api.fetch_notifications(client, state)

    # 4. Merge, deduplicate, filter
    events = merge_and_filter(
        all_feed_events,
        all_api_events,
        notification_events,
        config.events,
    )

    if not events:
        log.debug("No new events to post")
        state.touch_poll()
        state.save()
        return

    # 5. Render and post each event
    posted = 0
    for event in events:
        text = renderer.render(event)
        if not text:
            continue

        visibility = config.events.visibility.get(
            _event_type_to_config_key(event.event_type),
            config.mastodon.default_visibility,
        )
        spoiler = config.events.content_warning.get(
            _event_type_to_config_key(event.event_type),
        )

        if config.daemon.dry_run:
            log.info("[DRY RUN] Would post:\n%s", text)
            state.record_event(event.event_id)
            posted += 1
        else:
            success = await masto.post(
                client, text,
                visibility=visibility,
                spoiler_text=spoiler,
            )
            if success:
                state.record_event(event.event_id)
                posted += 1
                # Throttle to avoid flooding
                if posted < len(events):
                    await asyncio.sleep(_POST_THROTTLE)
            else:
                log.error("Failed to post event %s", event.event_id)

    log.info("Posted %d/%d events", posted, len(events))
    state.touch_poll()
    state.save()


def _event_type_to_config_key(event_type) -> str:
    """Map EventType to config key for visibility/content_warning lookups."""
    from gh_masto_poster.github.events import _TYPE_TO_CONFIG_KEY
    return _TYPE_TO_CONFIG_KEY.get(event_type, "")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="gh-masto-poster",
        description="Bridge GitHub activity to Mastodon posts",
    )
    parser.add_argument(
        "-c", "--config",
        default="config.ini",
        help="Path to config file (default: config.ini)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log posts without actually posting to Mastodon",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        config.daemon.dry_run = True

    logging.basicConfig(
        level=getattr(logging, config.daemon.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    asyncio.run(run(config))


if __name__ == "__main__":
    main()
