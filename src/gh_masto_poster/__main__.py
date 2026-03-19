"""Entry point — daemon main loop, signal handling, CLI."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time

import httpx

from gh_masto_poster.config import AppConfig, load_config
from gh_masto_poster.github.api import GitHubAPI
from gh_masto_poster.github.events import merge_and_filter
from gh_masto_poster.github.feeds import fetch_feed_events
from gh_masto_poster.mastodon.poster import MastodonPoster
from gh_masto_poster.models import Event, RepoInfo
from gh_masto_poster.state import State
from gh_masto_poster.templates import TemplateRenderer

log = logging.getLogger("gh_masto_poster")

# How many API poll cycles between repo-list refreshes
_REPO_REFRESH_INTERVAL = 50

# Throttle delay between Mastodon posts (seconds)
_POST_THROTTLE = 10


async def run(config: AppConfig) -> None:
    """Main async loop with independent timers for feeds, API, and notifications."""
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
        api_poll_count = 0

        # Track when each source was last polled
        last_feed_poll = 0.0
        last_api_poll = 0.0
        last_notif_poll = 0.0

        log.info(
            "Starting gh-masto-poster daemon (feeds: %.0fs, API: %.0fs, notifications: %.0fs)",
            config.daemon.feed_interval,
            config.daemon.api_interval,
            config.daemon.notification_interval,
        )

        while not shutdown.is_set():
            try:
                now = time.monotonic()

                # Refresh repo list periodically
                if not repos or api_poll_count % _REPO_REFRESH_INTERVAL == 0:
                    repos = await _discover_repos(client, gh_api, config)

                feed_events: list[Event] = []
                api_events: list[Event] = []
                notif_events: list[Event] = []

                # Poll feeds (no rate limit, poll frequently)
                if now - last_feed_poll >= config.daemon.feed_interval:
                    for repo in repos:
                        feed_events.extend(await fetch_feed_events(
                            client, repo, state,
                            releases=config.events.enabled.get("releases", True),
                            commits=config.events.enabled.get("commits", True),
                            tags=config.events.enabled.get("tags", True),
                        ))
                    last_feed_poll = now

                # Poll API (rate-limited, poll less frequently)
                if now - last_api_poll >= config.daemon.api_interval:
                    if not gh_api.rate_low:
                        for repo in repos:
                            api_events.extend(
                                await gh_api.fetch_repo_events(client, repo, state)
                            )
                        api_poll_count += 1
                    else:
                        wait = gh_api.seconds_until_reset()
                        log.warning(
                            "Skipping API poll (rate limit low: %d/%d remaining, resets in %.0fs)",
                            gh_api.rate_remaining, gh_api.rate_limit, wait,
                        )
                    last_api_poll = now

                # Poll notifications (respects X-Poll-Interval from server)
                notif_interval = max(
                    config.daemon.notification_interval,
                    gh_api.poll_interval,
                )
                if now - last_notif_poll >= notif_interval:
                    if not gh_api.rate_low:
                        notif_events = await gh_api.fetch_notifications(client, state)
                    last_notif_poll = now

                # Merge, dedup, filter, post
                events = merge_and_filter(
                    feed_events, api_events, notif_events, config.events,
                )
                if events:
                    await _post_events(
                        client, config, masto, renderer, state, events,
                    )
                else:
                    state.touch_poll()
                    state.save()

            except Exception:
                log.exception("Error in poll cycle")

            # Sleep until the next source needs polling
            sleep_time = _next_sleep(
                config, gh_api, last_feed_poll, last_api_poll, last_notif_poll,
            )
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=sleep_time)
            except asyncio.TimeoutError:
                pass

    state.save()
    log.info("Daemon stopped")


def _next_sleep(
    config: AppConfig,
    gh_api: GitHubAPI,
    last_feed: float,
    last_api: float,
    last_notif: float,
) -> float:
    """Calculate seconds until the next source needs polling."""
    now = time.monotonic()
    notif_interval = max(config.daemon.notification_interval, gh_api.poll_interval)
    waits = [
        config.daemon.feed_interval - (now - last_feed),
        config.daemon.api_interval - (now - last_api),
        notif_interval - (now - last_notif),
    ]
    return max(1.0, min(waits))


async def _post_events(
    client: httpx.AsyncClient,
    config: AppConfig,
    masto: MastodonPoster,
    renderer: TemplateRenderer,
    state: State,
    events: list[Event],
) -> None:
    """Render and post events to Mastodon."""
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
                if posted < len(events):
                    await asyncio.sleep(_POST_THROTTLE)
            else:
                log.error("Failed to post event %s", event.event_id)

    log.info("Posted %d/%d events", posted, len(events))
    state.touch_poll()
    state.save()


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
