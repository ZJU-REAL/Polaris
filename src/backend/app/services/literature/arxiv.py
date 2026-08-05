"""arXiv Atom API 客户端：分类+关键词搜索（日期窗口、分页）、按 id 批量取元数据、PDF 下载。

礼貌限速：官方建议请求间隔 3 秒（缓存命中不占限速额度）。被限流时按 ``Retry-After``
或指数退避重试，见 :meth:`ArxivClient._request`——所有出网请求都走那一个出口。

另含分类 RSS「新鲜源」（``fetch_new``）：``rss.arxiv.org/rss/{category}`` 返回当天新公告，
即时无滞后——用于绕开关键词检索索引 3-5 天的滞后（增量同步搜不到最新论文的根因）。
"""

import asyncio
import logging
import random
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
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

# 值得重试的状态码。429 是标准限流；**403 也算**——arXiv 历史上对它认为过量的客户端
# 直接返 403 而不是 429，当成永久失败会让整批论文白白丢掉。5xx 是服务端抖动。
# 其余 4xx（如检索串写错的 400）立即抛，不能烧四次退避去撞同一堵墙。
_RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
# 单次退避上限：Retry-After 可能给出很大的值，voyage 任务不能被一个响应头挂住半天
_MAX_BACKOFF_SECONDS = 120.0


class ArxivError(RuntimeError):
    """arXiv 请求失败（重试后仍未成功）。"""


class ArxivRateLimitedError(ArxivError):
    """被 arXiv 限流，且重试次数耗尽。"""


def _parse_iso_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """解析 Retry-After：整数秒或 HTTP-date 两种形式都要认，解不出返回 None。"""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(int(raw)))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (dt - datetime.now(UTC)).total_seconds())


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


def _batch_is_fresh(batch_at: datetime | None) -> bool:
    """这批公告还算「今天的」吗。读缓存与写缓存**共用这一条判据**。

    解析不出批次日期时算新鲜：宁可照常抓一次，也不能因为一个判断失灵就再也不抓。
    """
    if batch_at is None:
        return True
    return batch_at.astimezone(UTC).date() >= datetime.now(UTC).date()


def _parse_rss(text: str) -> tuple[list[dict[str, Any]], datetime | None]:
    """解析 RSS，返回 (条目, **这一批的公告日期**)。

    批次日期取 channel 的 ``pubDate``（arXiv 自己声明这批是哪天的公告）。有了它，
    「抓早了拿到上一批」就能当场识别——否则那种失败完全无声：条目照样有几百条，
    只是全都是昨天的，去重之后一条不进，而每一步都报成功。
    """
    root = ET.fromstring(text)
    out: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        parsed = _parse_rss_item(item)
        if parsed is not None:
            out.append(parsed)
    batch_at: datetime | None = None
    channel = root.find("channel")
    raw = channel.findtext("pubDate") if channel is not None else None
    if raw:
        try:
            batch_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            batch_at = None
    return out, batch_at


def build_search_query(
    categories: list[str],
    keywords: list[str],
    since: datetime | None = None,
    until: datetime | None = None,
    exclude: list[str] | None = None,
) -> str:
    """拼 arXiv 检索式。``exclude`` 走 ANDNOT，直接在检索端把不想要的挡掉。"""
    parts: list[str] = []
    if categories:
        parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
    if keywords:
        parts.append("(" + " OR ".join(f'all:"{k}"' for k in keywords) + ")")
    if since or until:
        lo = since.strftime("%Y%m%d%H%M") if since else "000001010000"
        hi = until.strftime("%Y%m%d%H%M") if until else "999912312359"
        parts.append(f"submittedDate:[{lo} TO {hi}]")
    query = " AND ".join(parts) if parts else "all:*"
    terms = [t for t in (exclude or []) if str(t).strip()]
    if terms:
        query += "".join(f' ANDNOT all:"{t}"' for t in terms)
    return query


class ArxivClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        redis: Redis | None = None,
        min_interval: float = 3.0,
        page_size: int = 100,
        max_retries: int = 4,
        backoff_base: float = 2.0,
    ) -> None:
        settings = get_settings()
        self._client = client or httpx.AsyncClient(
            proxy=settings.outbound_proxy or None,
            timeout=30.0,
            follow_redirects=True,
            # arXiv 对不表明身份的客户端限得更狠，给一个能联系到人的 UA
            headers={"User-Agent": f"Polaris/1.0 (+mailto:{settings.openalex_mailto})"},
        )
        self._cache = ResponseCache(redis)
        # RSS 新鲜源用独立的短 TTL 缓存（3h），跨用户/项目共享当天新公告
        self._rss_cache = ResponseCache(redis, ttl=_RSS_CACHE_TTL)
        # 闸门放 Redis 上，让 api 与 worker 两个进程共享同一个间隔（见 MinIntervalLimiter）
        self._limiter = MinIntervalLimiter(min_interval, redis=redis)
        self._page_size = page_size
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base

    async def _request(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        """所有 arXiv 请求的唯一出口：限速 + 遇限流/超时退避重试。

        重试覆盖两类失败，都是生产上实际见过的：``export.arxiv.org`` 返回 **429**
        （三个库同步任务因此停在第一步），以及 ``ReadTimeout``（一个建库任务因此中断）。

        退避优先听 ``Retry-After``，没有就指数退避 + **full jitter**（api 与 worker 是两个
        进程、各自独立重试，加满抖动才能把它们错开）。退避的同时 ``penalize`` 共享限流器，
        否则同进程里别的协程会接着打过去，等于没退避。
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            await self._limiter.acquire()
            retry_after: float | None = None
            try:
                resp = await self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # 生产上真实见过 ReadTimeout 让整个建库任务停在第一步。超时和连接错误
                # 与限流同属「过一会儿再试就好」，同样重试。
                last_exc = e
                logger.warning("arxiv request failed (%s): %s", type(e).__name__, url)
            else:
                if resp.status_code not in _RETRY_STATUSES:
                    resp.raise_for_status()
                    return resp
                retry_after = _retry_after_seconds(resp)
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} from arXiv", request=resp.request, response=resp
                )
                # 生产上 arXiv 返回的是 429（实测），响应头留档以便后续调整判据
                logger.warning(
                    "arxiv throttled: status=%s url=%s attempt=%s/%s retry_after=%s headers=%s",
                    resp.status_code,
                    url,
                    attempt + 1,
                    self._max_retries,
                    retry_after,
                    dict(resp.headers),
                )
            delay = (
                min(_MAX_BACKOFF_SECONDS, retry_after)
                if retry_after is not None
                else random.uniform(0, min(_MAX_BACKOFF_SECONDS, self._backoff_base * 2**attempt))
            )
            logger.warning(
                "arxiv retry in %.1fs (attempt %s/%s): %s",
                delay,
                attempt + 1,
                self._max_retries,
                url,
            )
            await self._limiter.penalize(delay)
            await asyncio.sleep(delay)
        raise ArxivRateLimitedError(
            f"arXiv unavailable after {self._max_retries} attempts"
            f" ({type(last_exc).__name__}): {url}"
        ) from last_exc

    async def _query(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        key = cache_key("arxiv", "query", params)
        if (cached := await self._cache.get(key)) is not None:
            return cached
        resp = await self._request(API_URL, params=params)
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
        exclude: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """分类+关键词搜索（日期窗口、可排除词），自动翻页至 limit。"""
        query = build_search_query(categories or [], keywords or [], since, until, exclude)
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

    async def fetch_new(self, category: str) -> tuple[list[dict[str, Any]], datetime | None]:
        """抓一个分类的当天新公告（RSS /new，即时无索引滞后）。

        只返回 announce_type ∈ {new, cross} 的条目（replace/replace-cross 是旧论文更新，
        已在解析时剔除）；字段与 ``search`` 返回的 entry 对齐，便于复用入库逻辑。

        **失败原样上抛**（限流已在 :meth:`_request` 里重试过，走到这里是真失败）。
        这里曾经把任何异常吞成 []，于是「被限流」和「今天确实没有新论文」变成同一个
        结果，谁都分不出来。每日池是所有文献库的唯一供给，一次静默的空池等于全实验室
        当天颗粒无收却无人知晓——由调用方按分类记录成败，见
        :func:`app.services.daily_feed.fetch_new_by_category`。
        失败不写缓存，下次重试。
        """
        key = cache_key("arxiv", "rss_new", {"category": category})
        if (cached := await self._rss_cache.get(key)) is not None:
            cached_at = _parse_iso_dt(cached.get("batch_at"))
            # **读的时候也要查新鲜度**，不是只在写的时候查：昨天 23:50 存进来的那份，
            # 当时确实是「今天的」，跨过 UTC 零点它就成了旧批次——而缓存还有两小时
            # 才到期。只查写不查读，等于每天 00:00–03:00 UTC（北京 08:00–11:00）的探测
            # 全部拿到昨天那批，判定「今天那批还没出来」，白等一上午。
            if _batch_is_fresh(cached_at):
                return cached["entries"], cached_at
        resp = await self._request(RSS_URL_TEMPLATE.format(category=category))
        entries, batch_at = _parse_rss(resp.text)
        # **陈旧批次不写缓存**：调用方会每 15 分钟探一次直到当天那批出现，缓存住旧批次
        # 会让接下来三小时的探测全部拿到同一份，轮询就白做了。与「失败不写缓存」同理。
        if _batch_is_fresh(batch_at):
            await self._rss_cache.set(
                key, {"entries": entries, "batch_at": batch_at.isoformat() if batch_at else None}
            )
        return entries, batch_at

    async def fetch_by_ids(self, arxiv_ids: list[str]) -> list[dict[str, Any]]:
        """按 id 批量取元数据。"""
        if not arxiv_ids:
            return []
        ids = ",".join(normalize_arxiv_id(a) for a in arxiv_ids)
        return await self._query({"id_list": ids, "max_results": len(arxiv_ids)})

    async def download_pdf(self, arxiv_id: str) -> bytes:
        """下载 PDF 原始字节（不缓存，交由调用方落盘）。"""
        resp = await self._request(pdf_url(arxiv_id))
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()
