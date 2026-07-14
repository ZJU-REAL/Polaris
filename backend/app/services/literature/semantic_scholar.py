"""Semantic Scholar Graph API 客户端：paper 详情 / references / citations（引文雪球用）。

- 可选 ``POLARIS_S2_API_KEY``（x-api-key 头）；
- 令牌桶限流：免 key 档 100 req / 5 min 的 80%（80/300 ≈ 0.267 req/s）；
- 429 指数退避重试。
"""

import asyncio
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.literature.cache import ResponseCache, TokenBucket, cache_key

API_BASE = "https://api.semanticscholar.org/graph/v1"

PAPER_FIELDS = "title,abstract,year,venue,externalIds,url,citationCount,tldr,authors"

# 免 key 100 req / 5 min 的 80%
_FREE_RATE = 0.8 * 100 / 300
_MAX_RETRIES = 4


class SemanticScholarClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        redis: Redis | None = None,
        api_key: str | None = None,
        rate: float = _FREE_RATE,
        backoff_base: float = 2.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            proxy=get_settings().outbound_proxy or None, timeout=30.0
        )
        self._cache = ResponseCache(redis)
        self._api_key = api_key if api_key is not None else get_settings().s2_api_key
        self._bucket = TokenBucket(rate=rate, capacity=max(1.0, rate * 10))
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key} if self._api_key else {}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        key = cache_key("s2", path, params)
        if (cached := await self._cache.get(key)) is not None:
            return cached
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            await self._bucket.acquire()
            resp = await self._client.get(
                f"{API_BASE}{path}", params=params, headers=self._headers()
            )
            if resp.status_code == 429:  # 指数退避后重试
                last_exc = httpx.HTTPStatusError(
                    "429 Too Many Requests", request=resp.request, response=resp
                )
                await asyncio.sleep(self._backoff_base * (2**attempt))
                continue
            resp.raise_for_status()
            data = resp.json()
            await self._cache.set(key, data)
            return data
        raise last_exc or RuntimeError("semantic scholar request failed")

    async def get_paper(self, paper_id: str, fields: str = PAPER_FIELDS) -> dict[str, Any]:
        """paper_id 形如 "arXiv:2401.12345" / "DOI:10.x/y" / S2 hex id。"""
        return await self._get(f"/paper/{paper_id}", {"fields": fields})

    async def get_references(
        self, paper_id: str, *, limit: int = 100, fields: str = PAPER_FIELDS
    ) -> list[dict[str, Any]]:
        """该论文引用的文献（citedPaper 列表）。"""
        data = await self._get(f"/paper/{paper_id}/references", {"fields": fields, "limit": limit})
        return [row["citedPaper"] for row in data.get("data", []) if row.get("citedPaper")]

    async def get_citations(
        self, paper_id: str, *, limit: int = 100, fields: str = PAPER_FIELDS
    ) -> list[dict[str, Any]]:
        """引用该论文的文献（citingPaper 列表）。"""
        data = await self._get(f"/paper/{paper_id}/citations", {"fields": fields, "limit": limit})
        return [row["citingPaper"] for row in data.get("data", []) if row.get("citingPaper")]

    async def aclose(self) -> None:
        await self._client.aclose()
