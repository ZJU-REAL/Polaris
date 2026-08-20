"""Crossref REST client for DOI metadata, relations, and supplemental search."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.literature.cache import ResponseCache, cache_key

API_BASE = "https://api.crossref.org"


class CrossrefClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        redis: Redis | None = None,
        mailto: str | None = None,
    ) -> None:
        self._mailto = mailto if mailto is not None else get_settings().openalex_mailto
        user_agent = "Polaris/1.0"
        if self._mailto:
            user_agent += f" (mailto:{self._mailto})"
        self._client = client or httpx.AsyncClient(
            proxy=get_settings().outbound_proxy or None,
            timeout=30.0,
            headers={"User-Agent": user_agent},
        )
        self._cache = ResponseCache(redis)

    async def _get(
        self, path: str, extra_params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        params = dict(extra_params or {})
        if self._mailto:
            params["mailto"] = self._mailto
        key = cache_key("crossref", path, params)
        if (cached := await self._cache.get(key)) is not None:
            return cached or None
        response = await self._client.get(f"{API_BASE}{path}", params=params)
        if response.status_code == 404:
            await self._cache.set(key, {})
            return None
        response.raise_for_status()
        data = response.json()
        await self._cache.set(key, data)
        return data

    async def get_work(self, doi: str) -> dict[str, Any] | None:
        normalized = doi.strip().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        data = await self._get(f"/works/{quote(normalized, safe='')}")
        message = (data or {}).get("message")
        return message if isinstance(message, dict) else None

    async def search_works_page(
        self, query: str, *, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "query.bibliographic": query,
            "rows": max(1, min(limit, 100)),
            "cursor": cursor or "*",
        }
        data = await self._get("/works", params)
        message = (data or {}).get("message") or {}
        items = [item for item in message.get("items") or [] if isinstance(item, dict)]
        next_cursor = message.get("next-cursor")
        return items, str(next_cursor) if next_cursor and items else None

    async def aclose(self) -> None:
        await self._client.aclose()
