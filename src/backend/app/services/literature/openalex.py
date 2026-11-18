"""OpenAlex 客户端：按 DOI / arxiv id 反查元数据与被引数（mailto polite pool，免 key）。"""

import asyncio
import re
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.literature.cache import ResponseCache, TokenBucket, cache_key

API_BASE = "https://api.openalex.org"

# polite pool 名义上限 10 req/s；批量对齐会连续打，按一半留余量
_POLITE_RATE = 5.0
# 被限流（429）后再试几次
_RATE_LIMIT_RETRIES = 2

# arXiv 论文的 DataCite DOI 前缀
ARXIV_DOI_TEMPLATE = "10.48550/arXiv.{arxiv_id}"


_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})", re.I)
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)


def _arxiv_id_of(work: dict[str, Any], doi: str | None) -> str | None:
    """这条 work 对应的 arXiv 编号（有的话）：DOI 是 arXiv 的 DataCite DOI，或者某个
    收录位置的链接指向 arxiv.org。跨来源去重靠它认出「这是同一篇预印本」。"""
    if doi and (m := _ARXIV_DOI_RE.match(doi)):
        return m.group(1)
    locations = work.get("locations")
    for loc in locations if isinstance(locations, list) else []:
        if not isinstance(loc, dict):
            continue
        for key in ("landing_page_url", "pdf_url"):
            if isinstance(loc.get(key), str) and (m := _ARXIV_URL_RE.search(loc[key])):
                return m.group(1)
    return None


def _simplify(work: dict[str, Any]) -> dict[str, Any]:
    """OpenAlex 的 work → 本平台用的那几项。每一项都按「可能缺、可能是 null、类型可能
    不对」来读：一条记录写得怪不该让整页结果作废。"""
    primary_location = work.get("primary_location")
    primary_location = primary_location if isinstance(primary_location, dict) else {}
    primary_source = primary_location.get("source")
    primary_source = primary_source if isinstance(primary_source, dict) else {}
    inverted = work.get("abstract_inverted_index")
    abstract = None
    if isinstance(inverted, dict):
        tokens = [
            (int(position), str(token))
            for token, positions in inverted.items()
            if isinstance(positions, list)
            for position in positions
            if isinstance(position, int)
        ]
        if tokens:
            abstract = " ".join(token for _, token in sorted(tokens))
    raw_doi = work.get("doi")
    doi = (raw_doi if isinstance(raw_doi, str) else "").removeprefix("https://doi.org/") or None
    authorships = work.get("authorships")
    authorships = (
        [a for a in authorships if isinstance(a, dict)] if isinstance(authorships, list) else []
    )

    def _institutions(authorship: dict[str, Any]) -> list[str]:
        raw = authorship.get("institutions")
        return list(
            dict.fromkeys(
                str(inst["display_name"])
                for inst in (raw if isinstance(raw, list) else [])
                if isinstance(inst, dict) and inst.get("display_name")
            )
        )

    year = work.get("publication_year")
    published = work.get("publication_date")
    ids = work.get("ids")
    cited = work.get("cited_by_count")
    referenced = work.get("referenced_works")
    return {
        "openalex_id": work.get("id") if isinstance(work.get("id"), str) else None,
        "title": work.get("title") if isinstance(work.get("title"), str) else None,
        "abstract": abstract,
        "doi": doi,
        "arxiv_id": _arxiv_id_of(work, doi),
        "url": primary_location.get("landing_page_url") or raw_doi or None,
        "year": year if isinstance(year, int) and not isinstance(year, bool) else None,
        # ISO date，精确发表日期
        "published": published if isinstance(published, str) else None,
        "venue": primary_source.get("display_name"),
        "openalex_source_id": primary_source.get("id"),
        "issn_l": primary_source.get("issn_l"),
        "issns": primary_source.get("issn") or [],
        "cited_by_count": cited if isinstance(cited, int) and not isinstance(cited, bool) else 0,
        # 这篇引用了哪些 work（OpenAlex id）；按引用关系扩展候选时用
        "referenced_works": [r for r in referenced if isinstance(r, str)]
        if isinstance(referenced, list)
        else [],
        # PubMed 编号：OpenAlex 给的是链接形式 https://pubmed.ncbi.nlm.nih.gov/<id>
        "pmid": (
            str((ids if isinstance(ids, dict) else {}).get("pmid") or "")
            .rstrip("/")
            .rsplit("/", 1)[-1]
            or None
        ),
        # 作者 + 每位作者的机构（OpenAlex 结构化给出 authorships[].institutions）
        "authors": [
            {"name": str(a["author"]["display_name"]), "affiliations": _institutions(a)}
            for a in authorships
            if isinstance(a.get("author"), dict) and a["author"].get("display_name")
        ],
        # 发表机构（去重保序）
        "affiliations": list(dict.fromkeys(name for a in authorships for name in _institutions(a))),
    }


class OpenAlexClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        redis: Redis | None = None,
        mailto: str | None = None,
        api_key: str | None = None,
        rate: float = _POLITE_RATE,
        retry_delay: float = 1.5,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            proxy=get_settings().outbound_proxy or None, timeout=30.0
        )
        self._cache = ResponseCache(redis)
        self._mailto = mailto if mailto is not None else get_settings().openalex_mailto
        self._api_key = api_key
        # 令牌桶限流（#639 批量对齐补上；容量=rate*2 允许零星调用不等待）
        self._bucket = TokenBucket(rate=rate, capacity=max(1.0, rate * 2))
        self._retry_delay = retry_delay

    async def _get(
        self, path: str, extra_params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = dict(extra_params or {})
        if self._mailto:
            params["mailto"] = self._mailto
        if self._api_key:
            params["api_key"] = self._api_key
        key = cache_key("openalex", path, params)
        if (cached := await self._cache.get(key)) is not None:
            return cached or None  # 缓存的 {} 表示 404
        resp = None
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            await self._bucket.acquire()  # 限流只管真实出网；缓存命中不占令牌
            resp = await self._client.get(f"{API_BASE}{path}", params=params)
            # 免密钥时偶尔会被限流（实测一次检索二十来个请求里有一两个 429）：
            # 隔一两秒再来多半就过，不值得让这条查询白白失败
            if resp.status_code != 429 or attempt == _RATE_LIMIT_RETRIES:
                break
            await asyncio.sleep(self._retry_delay * (attempt + 1))
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

    async def get_by_pmid(self, pmid: str) -> dict[str, Any] | None:
        """按 PubMed 编号取 work 元数据；不存在返回 None（#821）。"""
        work = await self._get(f"/works/pmid:{pmid}")
        return _simplify(work) if work else None

    async def get_by_arxiv(self, arxiv_id: str) -> dict[str, Any] | None:
        """按 arxiv id 反查（经 DataCite DOI 10.48550/arXiv.<id>）。"""
        return await self.get_by_doi(ARXIV_DOI_TEMPLATE.format(arxiv_id=arxiv_id))

    async def search_works(
        self,
        query: str,
        *,
        limit: int = 5,
        start_year: int | None = None,
        end_year: int | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        """按标题/关键词全文检索 works（M5-C 引用核验的 S2 降级通道）。

        不给 ``sort`` 时 OpenAlex 按与查询的相关度排（``search=`` 的默认）。一页最多
        200 条，``limit`` 超过的部分不取。
        """
        params: dict[str, Any] = {"search": query, "per-page": max(1, min(int(limit), 200))}
        if sort:
            params["sort"] = sort
        filters: list[str] = []
        if start_year is not None:
            filters.append(f"from_publication_date:{start_year}-01-01")
        if end_year is not None:
            filters.append(f"to_publication_date:{end_year}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        data = await self._get("/works", params)
        results = (data or {}).get("results") or []
        return [_simplify(w) for w in results if isinstance(w, dict)]

    async def find_work(
        self, *, doi: str | None = None, arxiv_id: str | None = None, title: str | None = None
    ) -> dict[str, Any] | None:
        """找到一篇论文在 OpenAlex 里的记录（含它的参考文献清单）：DOI → arXiv 编号 →
        标题，依次试，找不到返回 None。"""
        if doi:
            work = await self._get(f"/works/doi:{doi}")
            if work:
                return _simplify(work)
        if arxiv_id:
            work = await self._get(f"/works/doi:{ARXIV_DOI_TEMPLATE.format(arxiv_id=arxiv_id)}")
            if work:
                return _simplify(work)
        if title:
            cleaned = re.sub(r"[^\w\s\-]", " ", title).strip()
            if cleaned:
                data = await self._get(
                    "/works", {"filter": f"title.search:{cleaned}", "per-page": 1}
                )
                results = (data or {}).get("results") or []
                if results and isinstance(results[0], dict):
                    return _simplify(results[0])
        return None

    async def works_by_ids(self, openalex_ids: list[str]) -> list[dict[str, Any]]:
        """按 OpenAlex id 批量取 work（一次最多 50 个，多了分批）。"""
        out: list[dict[str, Any]] = []
        short = [i.rsplit("/", 1)[-1] for i in openalex_ids if i]
        for start in range(0, len(short), 50):
            batch = short[start : start + 50]
            data = await self._get(
                "/works", {"filter": "openalex:" + "|".join(batch), "per-page": len(batch)}
            )
            results = (data or {}).get("results") or []
            out.extend(_simplify(w) for w in results if isinstance(w, dict))
        return out

    async def citing_works(self, openalex_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """引用了这篇论文的 work，按被引数从高到低。"""
        data = await self._get(
            "/works",
            {
                "filter": f"cites:{openalex_id.rsplit('/', 1)[-1]}",
                "sort": "cited_by_count:desc",
                "per-page": max(1, min(int(limit), 200)),
            },
        )
        return [_simplify(w) for w in (data or {}).get("results") or [] if isinstance(w, dict)]

    async def aclose(self) -> None:
        await self._client.aclose()
