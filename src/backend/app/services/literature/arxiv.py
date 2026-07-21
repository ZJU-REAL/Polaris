"""arXiv Atom API 客户端：分类+关键词搜索（日期窗口、分页）、按 id 批量取元数据、PDF 下载。

礼貌限速：官方建议请求间隔 3 秒（缓存命中不占限速额度）。

另含分类 RSS「新鲜源」（``fetch_new``）：``rss.arxiv.org/rss/{category}`` 返回当天新公告，
即时无滞后——用于绕开关键词检索索引 3-5 天的滞后（增量同步搜不到最新论文的根因）。
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.literature.cache import MinIntervalLimiter, ResponseCache, cache_key

logger = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"
RSS_URL_TEMPLATE = "https://rss.arxiv.org/rss/{category}"
PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"
ABS_URL_TEMPLATE = "https://arxiv.org/abs/{arxiv_id}"

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

# RSS 2.0 feed 命名空间（item 子元素多为无前缀的 RSS 默认元素，仅这两个带前缀）
_RSS_NS = {
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# RSS /new 每天更新一次、URL 只按分类（对所有用户/项目相同）→ 短 TTL 缓存跨用户共享，
# 当天能及时拿到新公告又不重复打 arXiv。3 小时。
_RSS_CACHE_TTL = 3 * 3600

# 只接纳当天首发/跨列表公告；replace / replace-cross 是旧论文更新，跳过。
_RSS_KEEP_TYPES = frozenset({"new", "cross"})

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


def _rss_field(item: ET.Element, path: str) -> str | None:
    node = item.find(path, _RSS_NS)
    if node is None or node.text is None:
        return None
    return " ".join(node.text.split())


def _parse_rss_item(item: ET.Element) -> dict[str, Any] | None:
    """解析一条 RSS item；非 new/cross（旧论文更新）或缺关键字段时返回 None。"""
    announce = _rss_field(item, "arxiv:announce_type")
    if announce not in _RSS_KEEP_TYPES:
        return None

    # arxiv_id 优先从 guid（oai:arXiv.org:2607.15380v1）取末段，回退 link 的 /abs/ URL
    guid = _rss_field(item, "guid")
    raw_id = guid.rsplit(":", 1)[-1] if guid else (_rss_field(item, "link") or "")
    arxiv_id = normalize_arxiv_id(raw_id)
    title = _rss_field(item, "title") or ""
    if not arxiv_id or not title:
        return None

    # description = "arXiv:<id> Announce Type: <t>  Abstract: <全文摘要>"，截 Abstract: 之后
    desc = _rss_field(item, "description") or ""
    abstract = desc.split("Abstract:", 1)[1].strip() if "Abstract:" in desc else None

    creator = _rss_field(item, "dc:creator") or ""
    authors = [{"name": n.strip()} for n in creator.split(",") if n.strip()] or None

    categories = [c.text.strip() for c in item.findall("category") if c.text and c.text.strip()]

    pubdate = _rss_field(item, "pubDate")
    published: str | None = None
    year: int | None = None
    if pubdate:
        try:
            dt = parsedate_to_datetime(pubdate)
            published = dt.isoformat()
            year = dt.year
        except (TypeError, ValueError, IndexError):
            pass

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "published": published,
        "updated": None,
        "year": year,
        "categories": categories,
        "primary_category": categories[0] if categories else None,
        "doi": None,
        "url": ABS_URL_TEMPLATE.format(arxiv_id=arxiv_id),
        "pdf_url": PDF_URL_TEMPLATE.format(arxiv_id=arxiv_id),
        "announce_type": announce,
    }


def _parse_rss(text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(text)
    out: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        parsed = _parse_rss_item(item)
        if parsed is not None:
            out.append(parsed)
    return out


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
        # RSS 新鲜源用独立的短 TTL 缓存（3h），跨用户/项目共享当天新公告
        self._rss_cache = ResponseCache(redis, ttl=_RSS_CACHE_TTL)
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

    async def fetch_new(self, category: str) -> list[dict[str, Any]]:
        """抓一个分类的当天新公告（RSS /new，即时无索引滞后）。

        只返回 announce_type ∈ {new, cross} 的条目（replace/replace-cross 是旧论文更新，
        已在解析时剔除）；字段与 ``search`` 返回的 entry 对齐，便于复用入库逻辑。
        网络/解析失败一律记 warning 并返回 []（不能让整步崩），且不写缓存以便下次重试。
        """
        key = cache_key("arxiv", "rss_new", {"category": category})
        if (cached := await self._rss_cache.get(key)) is not None:
            return cached
        try:
            await self._limiter.acquire()
            resp = await self._client.get(RSS_URL_TEMPLATE.format(category=category))
            resp.raise_for_status()
            entries = _parse_rss(resp.text)
        except Exception:  # noqa: BLE001 — 新鲜源尽力而为；CancelledError 是 BaseException 不在此
            logger.warning("arxiv RSS fetch/parse failed for %s", category, exc_info=True)
            return []
        await self._rss_cache.set(key, entries)
        return entries

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
