"""arXiv Atom API 客户端：分类+关键词搜索（日期窗口、分页）、按 id 批量取元数据、PDF 下载。

礼貌限速：官方建议请求间隔 3 秒（缓存命中不占限速额度）。
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.literature.cache import MinIntervalLimiter, ResponseCache, cache_key

API_URL = "https://export.arxiv.org/api/query"
PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"
ABS_URL_TEMPLATE = "https://arxiv.org/abs/{arxiv_id}"

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

_VERSION_RE = re.compile(r"v\d+$")


def normalize_arxiv_id(raw: str) -> str:
    """从 URL / 带版本号的 id 提取裸 arxiv id（如 2401.12345）。"""
    arxiv_id = raw.rsplit("/abs/", 1)[-1].strip()
    return _VERSION_RE.sub("", arxiv_id)


def pdf_url(arxiv_id: str) -> str:
    return PDF_URL_TEMPLATE.format(arxiv_id=normalize_arxiv_id(arxiv_id))


def _text(entry: ET.Element, path: str) -> str | None:
    node = entry.find(path, _NS)
    if node is None or node.text is None:
        return None
    return " ".join(node.text.split())


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    raw_id = _text(entry, "atom:id") or ""
    arxiv_id = normalize_arxiv_id(raw_id)
    published = _text(entry, "atom:published")
    doi = _text(entry, "arxiv:doi")
    categories = [c.get("term", "") for c in entry.findall("atom:category", _NS) if c.get("term")]
    return {
        "arxiv_id": arxiv_id,
        "title": _text(entry, "atom:title") or "",
        "abstract": _text(entry, "atom:summary"),
        "authors": [
            {"name": name}
            for a in entry.findall("atom:author", _NS)
            if (name := _text(a, "atom:name"))
        ],
        "published": published,
        "updated": _text(entry, "atom:updated"),
        "year": int(published[:4]) if published else None,
        "categories": categories,
        "primary_category": categories[0] if categories else None,
        "doi": doi,
        "url": ABS_URL_TEMPLATE.format(arxiv_id=arxiv_id),
        "pdf_url": PDF_URL_TEMPLATE.format(arxiv_id=arxiv_id),
    }


def build_search_query(
    categories: list[str],
    keywords: list[str],
    since: datetime | None = None,
    until: datetime | None = None,
) -> str:
    parts: list[str] = []
    if categories:
        parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
    if keywords:
        parts.append("(" + " OR ".join(f'all:"{k}"' for k in keywords) + ")")
    if since or until:
        lo = since.strftime("%Y%m%d%H%M") if since else "000001010000"
        hi = until.strftime("%Y%m%d%H%M") if until else "999912312359"
        parts.append(f"submittedDate:[{lo} TO {hi}]")
    return " AND ".join(parts) if parts else "all:*"


class ArxivClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        redis: Redis | None = None,
        min_interval: float = 3.0,
        page_size: int = 100,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            proxy=get_settings().outbound_proxy or None, timeout=30.0, follow_redirects=True
        )
        self._cache = ResponseCache(redis)
        self._limiter = MinIntervalLimiter(min_interval)
        self._page_size = page_size

    async def _query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        key = cache_key("arxiv", "query", params)
        if (cached := await self._cache.get(key)) is not None:
            return cached
        await self._limiter.acquire()
        resp = await self._client.get(API_URL, params=params)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        entries = [_parse_entry(e) for e in root.findall("atom:entry", _NS)]
        await self._cache.set(key, entries)
        return entries

    async def search(
        self,
        *,
        categories: list[str] | None = None,
        keywords: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """分类+关键词搜索（日期窗口），自动翻页至 limit。"""
        query = build_search_query(categories or [], keywords or [], since, until)
        results: list[dict[str, Any]] = []
        start = 0
        while len(results) < limit:
            batch = min(self._page_size, limit - len(results))
            entries = await self._query(
                {
                    "search_query": query,
                    "start": start,
                    "max_results": batch,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                }
            )
            results.extend(entries)
            if len(entries) < batch:  # 已到末页
                break
            start += len(entries)
        return results[:limit]

    async def fetch_by_ids(self, arxiv_ids: list[str]) -> list[dict[str, Any]]:
        """按 id 批量取元数据。"""
        if not arxiv_ids:
            return []
        ids = ",".join(normalize_arxiv_id(a) for a in arxiv_ids)
        return await self._query({"id_list": ids, "max_results": len(arxiv_ids)})

    async def download_pdf(self, arxiv_id: str) -> bytes:
        """下载 PDF 原始字节（不缓存，交由调用方落盘）。"""
        await self._limiter.acquire()
        resp = await self._client.get(pdf_url(arxiv_id))
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()
