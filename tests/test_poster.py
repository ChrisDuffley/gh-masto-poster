"""Tests for the Mastodon poster."""

import httpx
import pytest
import respx

from gh_masto_poster.mastodon.poster import MastodonPoster


@pytest.mark.asyncio
async def test_post_success() -> None:
    poster = MastodonPoster("https://mastodon.example", "token123")

    with respx.mock:
        respx.post("https://mastodon.example/api/v1/statuses").respond(
            200,
            json={"id": "12345", "content": "<p>Hello</p>"},
            headers={"x-ratelimit-remaining": "299"},
        )

        async with httpx.AsyncClient() as client:
            result = await poster.post(client, "Hello world!")

    assert result is True
    assert poster.rate_remaining == 299


@pytest.mark.asyncio
async def test_post_empty_status() -> None:
    poster = MastodonPoster("https://mastodon.example", "token123")

    async with httpx.AsyncClient() as client:
        result = await poster.post(client, "")

    assert result is False


@pytest.mark.asyncio
async def test_post_422_rejected() -> None:
    poster = MastodonPoster("https://mastodon.example", "token123")

    with respx.mock:
        respx.post("https://mastodon.example/api/v1/statuses").respond(
            422, json={"error": "Validation failed"},
        )

        async with httpx.AsyncClient() as client:
            result = await poster.post(client, "Bad post")

    assert result is False


@pytest.mark.asyncio
async def test_detect_character_limit() -> None:
    poster = MastodonPoster("https://mastodon.example", "token123")

    with respx.mock:
        respx.get("https://mastodon.example/api/v2/instance").respond(
            200,
            json={
                "configuration": {
                    "statuses": {"max_characters": 1000},
                },
            },
        )

        async with httpx.AsyncClient() as client:
            limit = await poster.detect_character_limit(client)

    assert limit == 1000


@pytest.mark.asyncio
async def test_detect_character_limit_fallback() -> None:
    poster = MastodonPoster("https://mastodon.example", "token123")

    with respx.mock:
        respx.get("https://mastodon.example/api/v2/instance").respond(500)

        async with httpx.AsyncClient() as client:
            limit = await poster.detect_character_limit(client)

    assert limit == 500


@pytest.mark.asyncio
async def test_visibility_and_spoiler() -> None:
    poster = MastodonPoster("https://mastodon.example", "token123", default_visibility="unlisted")

    with respx.mock:
        route = respx.post("https://mastodon.example/api/v1/statuses").respond(200, json={"id": "1"})

        async with httpx.AsyncClient() as client:
            await poster.post(client, "Test", visibility="private", spoiler_text="CW")

    # Check the posted data
    request = route.calls[0].request
    body = request.content.decode()
    assert "private" in body
    assert "CW" in body
