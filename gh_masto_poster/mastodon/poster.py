"""Mastodon API client — post statuses with rate-limit handling."""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


class MastodonPoster:
    """Posts statuses to a Mastodon instance with rate-limit awareness."""

    def __init__(
        self,
        instance_url: str,
        access_token: str,
        default_visibility: str = "public",
    ) -> None:
        self._base = instance_url.rstrip("/")
        self._token = access_token
        self._default_visibility = default_visibility
        self._rate_remaining: int = 300
        self._rate_reset: float = 0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def detect_character_limit(self, client: httpx.AsyncClient) -> int:
        """Auto-detect the instance's character limit. Returns 500 on failure."""
        try:
            resp = await client.get(
                f"{self._base}/api/v2/instance",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                limit = (
                    data.get("configuration", {})
                    .get("statuses", {})
                    .get("max_characters", 500)
                )
                log.info("Mastodon character limit: %d", limit)
                return int(limit)
        except Exception:
            log.debug("Failed to detect character limit, using 500")
        return 500

    async def post(
        self,
        client: httpx.AsyncClient,
        status: str,
        *,
        visibility: str | None = None,
        spoiler_text: str | None = None,
    ) -> bool:
        """Post a status to Mastodon. Returns True on success."""
        if not status.strip():
            return False

        data: dict[str, str] = {
            "status": status,
            "visibility": visibility or self._default_visibility,
        }
        if spoiler_text:
            data["spoiler_text"] = spoiler_text

        # Retry with exponential backoff
        for attempt in range(4):
            try:
                resp = await client.post(
                    f"{self._base}/api/v1/statuses",
                    headers=self._headers(),
                    data=data,
                )
                self._update_rate_info(resp)

                if resp.status_code == 200:
                    log.info("Posted to Mastodon: %s…", status[:60])
                    return True

                if resp.status_code == 429:
                    wait = _retry_after(resp, default=30 * (2 ** attempt))
                    log.warning("Rate limited by Mastodon, waiting %.0fs", wait)
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code == 422:
                    log.error("Mastodon rejected post (422): %s", resp.text)
                    return False

                if resp.status_code >= 500:
                    wait = 5 * (2 ** attempt)
                    log.warning("Mastodon server error (%d), retrying in %ds", resp.status_code, wait)
                    await asyncio.sleep(wait)
                    continue

                log.error("Mastodon post failed (%d): %s", resp.status_code, resp.text)
                return False

            except httpx.HTTPError as exc:
                wait = 5 * (2 ** attempt)
                log.warning("HTTP error posting to Mastodon: %s, retrying in %ds", exc, wait)
                await asyncio.sleep(wait)

        log.error("Failed to post after retries: %s…", status[:60])
        return False

    def _update_rate_info(self, resp: httpx.Response) -> None:
        if "x-ratelimit-remaining" in resp.headers:
            self._rate_remaining = int(resp.headers["x-ratelimit-remaining"])
        if "x-ratelimit-reset" in resp.headers:
            try:
                self._rate_reset = float(resp.headers["x-ratelimit-reset"])
            except ValueError:
                pass

    @property
    def rate_remaining(self) -> int:
        return self._rate_remaining


def _retry_after(resp: httpx.Response, default: float) -> float:
    """Extract Retry-After from response, or use default."""
    val = resp.headers.get("retry-after")
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return default
