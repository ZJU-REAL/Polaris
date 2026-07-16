"""OpenAlex 客户端：按 DOI / arxiv id 反查元数据与被引数（mailto polite pool，免 key）。"""

from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.literature.cache import ResponseCache, cache_key

API_BASE = "https://api.openalex.org"

# arXiv 论文的 DataCite DOI 前缀
ARXIV_DOI_TEMPLATE = "10.48550/arXiv.{arxiv_id}"


def _simplify(work: dict[str, Any]) -> dict[str, Any]:
    primary_location = work.get("primary_location") or {}
    return {
        "openalex_id": work.get("id"),
        "title": work.get("title"),
        "doi": (work.get("doi") or "").removeprefix("https://doi.org/") or None,
        "url": (
            primary_location.get("landing_page_url") if isinstance(primary_location, dict) else None
        )
        or work.get("doi")
        or None,
        "year": work.get("publication_year"),
        "venue": (work.get("primary_location") or {}).get("source", {}).get("display_name")
        if isinstance((work.get("primary_location") or {}).get("source"), dict)
        else None,
        "cited_by_count": work.get("cited_by_count", 0),
        "authors": [
            {"name": a.get("author", {}).get("display_name")}
            for a in work.get("authorships", [])
            if a.get("author", {}).get("display_name")
        ],
        # 发表机构（去重保序）：authorships[].institutions[].display_name
        "affiliations": list(
            dict.fromkeys(
                inst.get("display_name")
                for a in work.get("authorships", [])
                for inst in (a.get("institutions") or [])
                if isinstance(inst, dict) and inst.get("display_name")
            )
        ),
    }


class OpenAlexClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        redis: Redis | None = None,
        mailto: str | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            proxy=get_settings().outbound_proxy or None, timeout=30.0
        )
        self._cache = ResponseCache(redis)
        self._mailto = mailto if mailto is not None else get_settings().openalex_mailto

    async def _get(
        self, path: str, extra_params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = dict(extra_params or {})
        if self._mailto:
            params["mailto"] = self._mailto
        key = cache_key("openalex", path, params)
        if (cached := await self._cache.get(key)) is not None:
            return cached or None  # 缓存的 {} 表示 404
        resp = await self._client.get(f"{API_BASE}{path}", params=params)
        if resp.status_code == 404:
            await self._cache.set(key, {})
            return None
        resp.raise_for_status()
        data = resp.json()
        await self._cache.set(key, data)
        return data

    async def get_by_doi(self, doi: str) -> dict[str, Any] | None:
        """按 DOI 取 work 元数据（含 cited_by_count）；不存在返回 None。"""
        work = await self._get(f"/works/doi:{doi}")
        return _simplify(work) if work else None

    async def get_by_arxiv(self, arxiv_id: str) -> dict[str, Any] | None:
        """按 arxiv id 反查（经 DataCite DOI 10.48550/arXiv.<id>）。"""
        return await self.get_by_doi(ARXIV_DOI_TEMPLATE.format(arxiv_id=arxiv_id))

    async def search_works(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """按标题/关键词全文检索 works（M5-C 引用核验的 S2 降级通道）。"""
        data = await self._get("/works", {"search": query, "per-page": limit})
        results = (data or {}).get("results") or []
        return [_simplify(w) for w in results if isinstance(w, dict)]

    async def aclose(self) -> None:
        await self._client.aclose()
