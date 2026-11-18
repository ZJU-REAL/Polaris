"""Small async clients for the public providers used by the discovery runtime.

The provider payloads are intentionally normalized here instead of leaking
source-specific JSON into the worker.  Unpaywall is an OA resolver (DOI
lookup), so its keyword-search method returns no candidates by design.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from itertools import count
from threading import Lock
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_KEY_INDEX: dict[str, count] = {}
_KEY_LOCK = Lock()


class ProviderRequestError(RuntimeError):
    """A provider request failed after bounded retries."""

    def __init__(self, source: str, code: str, detail: str, *, retryable: bool = True) -> None:
        super().__init__(detail)
        self.source = source
        self.code = code
        self.retryable = retryable


def provider_failure_detail(source: str, error: Exception | None) -> tuple[str, str]:
    """Return a stable provider failure without URLs, query strings, or headers."""

    if isinstance(error, ProviderRequestError):
        code = error.code
    elif isinstance(error, httpx.HTTPStatusError):
        code = f"HTTP_{error.response.status_code}"
    elif isinstance(error, httpx.TimeoutException):
        code = "TIMEOUT"
    elif isinstance(error, httpx.RequestError):
        code = "NETWORK_ERROR"
    elif isinstance(error, ValueError):
        code = "INVALID_RESPONSE"
    elif error is not None:
        code = type(error).__name__.upper()
    else:
        code = "REQUEST_FAILED"
    return code, f"{source} provider request failed ({code})"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(_text(item) for item in value if _text(item))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value))).strip()


def _first(value: Any) -> str:
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    return _text(value)


def _doi(value: Any) -> str | None:
    value = _text(value)
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    return value or None


def _year(value: Any) -> int | None:
    """从任意形状的值里取年份：整数、"2024-05-01"、[2024, 5]、{"date-parts": [[2024]]}……
    取不出来返回 None，绝不抛错——一条记录的日期写得怪，不该让整个来源失败。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1000 <= value <= 3000 else None
    if isinstance(value, float):
        return _year(int(value)) if value == value and abs(value) < 1e6 else None
    if isinstance(value, Mapping):
        return _year(value.get("date-parts") or value.get("date-time") or value.get("year"))
    if isinstance(value, list | tuple):
        return _year(value[0]) if value else None
    match = re.search(r"\b(?:1[5-9]|20)\d{2}\b", _text(value))
    return int(match.group(0)) if match else None


def date_parts_iso(value: Any) -> str | None:
    """Crossref 式的日期（``{"date-parts": [[2024, 5, 1]]}``，月、日可缺，可能是 null
    或字符串）→ ISO 日期字符串；缺的月、日按 1 补。认不出来返回 None。"""
    parts = value.get("date-parts") if isinstance(value, Mapping) else value
    if isinstance(parts, list | tuple) and parts and isinstance(parts[0], list | tuple):
        parts = parts[0]
    if not isinstance(parts, list | tuple) or not parts:
        return None
    numbers: list[int] = []
    for part in list(parts)[:3]:
        try:
            numbers.append(int(str(part).strip()))
        except (TypeError, ValueError):
            break
    if not numbers or not 1000 <= numbers[0] <= 3000:
        return None
    month = numbers[1] if len(numbers) > 1 and 1 <= numbers[1] <= 12 else 1
    day = numbers[2] if len(numbers) > 2 and 1 <= numbers[2] <= 31 else 1
    try:
        return datetime(numbers[0], month, day, tzinfo=UTC).date().isoformat()
    except ValueError:
        return datetime(numbers[0], month, 1, tzinfo=UTC).date().isoformat()


def _int_or_none(value: Any) -> int | None:
    """计数类字段：不是非负整数就当没有（有的来源给字符串，有的给 null）。"""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _authors(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"name": part.strip()} for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            name = _text(item.get("name") or item.get("fullName") or item.get("authorName"))
        else:
            name = _text(item)
        if name:
            result.append({"name": name})
    return result


def _date_window(request: Any) -> tuple[int | None, int | None]:
    return request.start_year, request.end_year


def _within_year(row: Mapping[str, Any], start: int | None, end: int | None) -> bool:
    year = row.get("year")
    if not isinstance(year, int) or isinstance(year, bool):
        return True
    return (start is None or year >= start) and (end is None or year <= end)


def _safe_rows(source: str, items: Any, convert: Any) -> list[dict[str, Any]]:
    """逐条转换来源的原始记录；某一条转换出错就跳过那一条。

    以前是一个列表推导：一条记录字段形状不对（作者是字符串、日期是 null……）就整批
    抛错，这个来源这次一篇都拿不到。"""
    rows: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            row = convert(item)
        except Exception:  # noqa: BLE001 — 单条记录坏了不拖累整个来源
            logger.warning("skipped a malformed %s record", source, exc_info=True)
            continue
        row["year"] = _year(row.get("year"))
        if "citation_count" in row:
            row["citation_count"] = _int_or_none(row.get("citation_count"))
        rows.append(row)
    return rows


def _pubmed_abstracts(xml_text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return result
    for article in root.findall(".//PubmedArticle"):
        pmid = _text(article.findtext(".//MedlineCitation/PMID"))
        parts: list[str] = []
        for node in article.findall(".//Article/Abstract/AbstractText"):
            text = _text("".join(node.itertext()))
            label = _text(node.attrib.get("Label"))
            if text:
                parts.append(f"{label}: {text}" if label else text)
        if pmid and parts:
            result[pmid] = "\n".join(parts)
    return result


#: 每日池向 PubMed 回看几天。见 MultiSourceClient.fetch_new_pubmed 的理由：
#: 一天的窗口在凌晨跑时几乎总是空的，而漏掉的那天补不回来。
PUBMED_DAILY_WINDOW_DAYS = 2


class MultiSourceClient:
    """HTTP client for the non-core providers in the YFR-compatible source set."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        provider_keys: Mapping[str, list[str]] | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client or httpx.AsyncClient(
            proxy=settings.outbound_proxy or None,
            timeout=settings.literature_source_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": f"Polaris/1.0 (mailto:{settings.openalex_mailto})"},
        )
        self._owns_client = client is None
        self._timeout = settings.literature_source_timeout_seconds
        self._retries = settings.literature_source_retries
        self._provider_keys = {
            str(source).strip().lower(): tuple(
                str(value).strip() for value in values if str(value).strip()
            )
            for source, values in (provider_keys or {}).items()
        }

    def _key(self, source: str, fallback: str | list[str] = "") -> str:
        configured = source in self._provider_keys
        pool = self._provider_keys.get(source, ())
        if not configured:
            values = fallback if isinstance(fallback, list) else re.split(r"[,;\s]+", fallback)
            pool = tuple(str(value).strip() for value in values if str(value).strip())
        if not pool:
            return ""
        with _KEY_LOCK:
            index = next(_KEY_INDEX.setdefault(source, count()))
        return pool[index % len(pool)]

    async def _request_json(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Request JSON with bounded retry and explicit provider errors."""
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout or self._timeout,
                )
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    response.raise_for_status()
                if response.status_code >= 400:
                    raise ProviderRequestError(
                        source,
                        f"HTTP_{response.status_code}",
                        f"{source} returned HTTP {response.status_code}",
                        retryable=False,
                    )
                return response.json()
            except ProviderRequestError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            if attempt < self._retries:
                await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
        code, detail = provider_failure_detail(source, last_error)
        raise ProviderRequestError(source, code, detail) from last_error

    async def _request_text(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(
                    method, url, params=params, timeout=self._timeout
                )
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < self._retries:
                await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
        code, detail = provider_failure_detail(source, last_error)
        raise ProviderRequestError(source, code, detail) from last_error

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_source(self, source: str, request: Any) -> list[dict[str, Any]]:
        handlers = {
            "pubmed": self._search_pubmed,
            "crossref": self._search_crossref,
            "europepmc": self._search_europepmc,
            "hal": self._search_hal,
            "core": self._search_core,
            "base": self._search_base,
            "sciverse": self._search_sciverse,
        }
        handler = handlers.get(source.strip().lower())
        rows = await handler(request) if handler else []
        start, end = _date_window(request)
        return [row for row in rows if _within_year(row, start, end)]

    async def lookup_unpaywall(self, doi: str) -> dict[str, Any] | None:
        settings = get_settings()
        if not settings.unpaywall_email or not doi:
            return None
        try:
            payload = await self._request_json(
                "unpaywall",
                "GET",
                f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}",
                params={"email": settings.unpaywall_email},
                timeout=min(15.0, self._timeout),
            )
        except ProviderRequestError:
            return None
        return payload if isinstance(payload, dict) else None

    async def fetch_new_pubmed(
        self, term: str, *, days: int = PUBMED_DAILY_WINDOW_DAYS, limit: int = 200
    ) -> list[dict[str, Any]]:
        """PubMed 近几天**新进库**的记录，供每日池（#778）。

        用 ``[edat]``（Entrez 收录日）而不是 ``[pdat]``（出版日）：出版日是期刊印在
        论文上的日期，一篇 2025 年 12 月出版的文章可能 2026 年 3 月才进 PubMed，
        按出版日查「今天」会把它永远漏掉。收录日才是 arXiv「今天公告了什么」的对应物。

        窗口默认两天而不是一天：PubMed 全天滚动收录，凌晨跑的话当天几乎是空的；
        而每日池是所有文献库的唯一供给，漏掉的那天补不回来。重复看到昨天的记录无害
        ——入池按论文身份去重，已有条目只会合并分类、不动 feed_date。
        """
        until = datetime.now(UTC).date()
        since = until - timedelta(days=max(days - 1, 0))
        query = f"({term}) AND ({since:%Y/%m/%d}:{until:%Y/%m/%d}[edat])"
        return await self._pubmed_rows(query, limit=limit, sort="date")

    async def _search_pubmed(self, request: Any) -> list[dict[str, Any]]:
        start, end = _date_window(request)
        query = request.query
        if start or end:
            lo = start or 1800
            hi = end or datetime.now(UTC).year
            query = f"({query}) AND ({lo}:{hi}[pdat])"
        return await self._pubmed_rows(query, limit=request.limit, sort="relevance")

    async def _pubmed_rows(
        self,
        query: str,
        *,
        limit: int,
        sort: str,
    ) -> list[dict[str, Any]]:
        """esearch → esummary → efetch 三段式，检索与日更共用。

        抽出来是因为日更要的只是**换一个 term 与排序**；复制一份的话，PubMed 改了
        字段或者密钥参数加了一项，两边就会不声不响地走岔。
        """
        settings = get_settings()
        api_key = self._key("pubmed", settings.pubmed_api_key)
        params = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": min(limit, 1000),
            "sort": sort,
        }
        if api_key:
            params["api_key"] = api_key
        if settings.pubmed_email:
            params["email"] = settings.pubmed_email
        try:
            payload = await self._request_json(
                "pubmed",
                "GET",
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params=params,
            )
            ids = (payload.get("esearchresult") or {}).get("idlist") or []
            if not ids:
                return []
            summary_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
            if api_key:
                summary_params["api_key"] = api_key
            summary = await self._request_json(
                "pubmed",
                "GET",
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params=summary_params,
            )
            result = summary.get("result") or {}
            fetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
            if api_key:
                fetch_params["api_key"] = api_key
            try:
                abstracts = _pubmed_abstracts(
                    await self._request_text(
                        "pubmed",
                        "GET",
                        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                        params=fetch_params,
                    )
                )
            except ProviderRequestError:
                abstracts = {}
        except ProviderRequestError:
            raise
        rows: list[dict[str, Any]] = []
        for pmid in ids:
            item = result.get(str(pmid))
            if not isinstance(item, dict) or not item.get("title"):
                continue
            raw_ids = item.get("articleids")
            article_ids = {
                str(article.get("idtype")): article.get("value")
                for article in (raw_ids if isinstance(raw_ids, list) else [])
                if isinstance(article, dict)
            }
            rows.append(
                {
                    "source": "pubmed",
                    "pmid": str(pmid),
                    "title": _text(item.get("title")),
                    "abstract": abstracts.get(str(pmid)),
                    "authors": _authors(item.get("authors")),
                    "year": _year(item.get("pubdate")),
                    "venue": _text(item.get("fulljournalname") or item.get("source")),
                    "doi": _doi(article_ids.get("doi")),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "metadata": item,
                }
            )
        return rows

    async def _search_crossref(self, request: Any) -> list[dict[str, Any]]:
        settings = get_settings()
        # 期刊论文、会议论文、预印本都要：只留期刊论文的话，计算机方向的大半文献
        # （会议、arXiv）一篇都搜不到。同一个键写多次是「任一」
        filters = ["type:journal-article", "type:proceedings-article", "type:posted-content"]
        if request.start_year:
            filters.append(f"from-pub-date:{request.start_year}-01-01")
        if request.end_year:
            filters.append(f"until-pub-date:{request.end_year}-12-31")
        params = {
            # query.bibliographic 只对标题、作者、刊名、年份做匹配，按相关度排；
            # 笼统的 query 还会匹配基金、机构等字段，结果里混进大量不相干的记录
            "query.bibliographic": request.query,
            "rows": min(request.limit, 1000),
            "filter": ",".join(filters),
            "sort": "relevance",
            "order": "desc",
        }
        headers = {"User-Agent": f"Polaris/1.0 (mailto:{settings.crossref_mailto})"}
        try:
            payload = await self._request_json(
                "crossref", "GET", "https://api.crossref.org/works", params=params, headers=headers
            )
            items = (payload.get("message") or {}).get("items") or []
        except ProviderRequestError:
            raise
        return _safe_rows("crossref", items, self._crossref_item)

    async def _search_europepmc(self, request: Any) -> list[dict[str, Any]]:
        start, end = _date_window(request)
        lo, hi = start or 1800, end or datetime.now(UTC).year
        params = {
            "query": f"({request.query}) AND FIRST_PDATE:[{lo}-01-01 TO {hi}-12-31]",
            "format": "json",
            "resultType": "core",
            "pageSize": min(request.limit, 100),
            "sort": "CITED desc",
        }
        try:
            payload = await self._request_json(
                "europepmc",
                "GET",
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params=params,
            )
            items = (payload.get("resultList") or {}).get("result") or []
        except ProviderRequestError:
            raise
        return _safe_rows("europepmc", items, self._europepmc_item)

    async def _search_hal(self, request: Any) -> list[dict[str, Any]]:
        start, end = _date_window(request)
        lo, hi = start or 1800, end or datetime.now(UTC).year
        params = {
            "q": f"({request.query}) AND producedDateY_i:[{lo} TO {hi}]",
            "rows": min(request.limit, 100),
            "sort": "score desc",
            "fl": (
                "docid,title_s,authFullName_s,abstract_s,doiId_s,producedDateY_i,"
                "producedDate_s,journalTitle_s,uri_s,fileMain_s,fileAnnexes_s"
            ),
            "wt": "json",
        }
        try:
            payload = await self._request_json(
                "hal", "GET", "https://api.archives-ouvertes.fr/search/", params=params
            )
            items = (payload.get("response") or {}).get("docs") or []
        except ProviderRequestError:
            raise
        return _safe_rows("hal", items, self._hal_item)

    async def _search_core(self, request: Any) -> list[dict[str, Any]]:
        settings = get_settings()
        api_key = self._key("core", settings.core_api_key)
        if not api_key:
            return []
        year_filters = [f"yearPublished>={request.start_year or 1800}"]
        if request.end_year is not None:
            year_filters.append(f"yearPublished<={request.end_year}")
        params = {
            "q": f"{request.query} {' '.join(year_filters)}",
            "limit": min(request.limit, 100),
        }
        try:
            payload = await self._request_json(
                "core",
                "POST",
                "https://api.core.ac.uk/v3/search/works",
                json=params,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            items = payload.get("results") or []
        except ProviderRequestError:
            raise
        return _safe_rows("core", items, self._core_item)

    async def _search_base(self, request: Any) -> list[dict[str, Any]]:
        start, end = _date_window(request)
        params = {
            "func": "PerformSearch",
            "query": f"{request.query} year:{start or 1800}-{end or datetime.now(UTC).year}",
            "format": "json",
            "hits": min(request.limit, 100),
        }
        try:
            payload = await self._request_json(
                "base",
                "GET",
                "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi",
                params=params,
            )
            items = (payload.get("response") or {}).get("docs") or payload.get("docs") or []
        except ProviderRequestError:
            raise
        return _safe_rows("base", items, self._base_item)

    async def _search_sciverse(self, request: Any) -> list[dict[str, Any]]:
        settings = get_settings()
        token = self._key("sciverse", settings.sciverse_api_tokens)
        if not token:
            return []
        try:
            payload = await self._request_json(
                "sciverse",
                "POST",
                f"{settings.sciverse_base_url.rstrip('/')}/meta-search",
                json={
                    "query": request.query,
                    "page_size": min(request.limit, 100),
                    "collection": "papers",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except ProviderRequestError:
            raise
        items = payload.get("results") or payload.get("items") or payload.get("data") or []
        return _safe_rows("sciverse", items, self._sciverse_item)

    @staticmethod
    def _crossref_item(item: Mapping[str, Any]) -> dict[str, Any]:
        # 日期在这几个字段里任取一个有值的；每个都可能缺、是 null，或者只有年份
        published = next(
            (
                iso
                for key in ("published-print", "published-online", "published", "issued")
                if (iso := date_parts_iso(item.get(key)))
            ),
            None,
        )
        year = int(published[:4]) if published else _year(item.get("created"))
        authors = []
        raw_authors = item.get("author")
        for author in raw_authors if isinstance(raw_authors, list) else []:
            if not isinstance(author, Mapping):
                continue
            name = _text(
                author.get("name")
                or " ".join(str(p) for p in (author.get("given"), author.get("family")) if p)
            )
            if name:
                authors.append({"name": name})
        raw_links = item.get("link")
        pdf_url = next(
            (
                _text(link.get("URL"))
                for link in (raw_links if isinstance(raw_links, list) else [])
                if isinstance(link, dict) and "pdf" in _text(link.get("content-type")).lower()
            ),
            None,
        )
        metadata = dict(item)
        # 下游按 metadata["published"] 取发表日期，要的是 ISO 字符串；Crossref 原始的
        # published 是 {"date-parts": …}，原样留着会在解析日期时出错
        metadata["published"] = published
        return {
            "source": "crossref",
            "title": _first(item.get("title")),
            "abstract": _text(item.get("abstract")),
            "authors": authors,
            "year": year,
            "venue": _first(item.get("container-title")),
            "doi": _doi(item.get("DOI")),
            "url": _text(item.get("URL")) or None,
            "pdf_url": pdf_url,
            "oa_status": "oa" if pdf_url else None,
            "citation_count": item.get("is-referenced-by-count"),
            "metadata": metadata,
        }

    @staticmethod
    def _europepmc_item(item: Mapping[str, Any]) -> dict[str, Any]:
        url_list = item.get("fullTextUrlList")
        links = (url_list.get("fullTextUrl") if isinstance(url_list, Mapping) else None) or []
        links = links if isinstance(links, list) else []
        pdf = next(
            (
                _text(link.get("url"))
                for link in links
                if isinstance(link, dict) and _text(link.get("documentStyle")).lower() == "pdf"
            ),
            None,
        )
        return {
            "source": "europepmc",
            "title": _text(item.get("title")),
            "abstract": _text(item.get("abstractText")),
            "authors": _authors(item.get("authorString")),
            "year": _year(item.get("firstPublicationDate") or item.get("pubYear")),
            "venue": _text(item.get("journalTitle")),
            "doi": _doi(item.get("doi")),
            "pmid": _text(item.get("pmid")) or None,
            "url": f"https://europepmc.org/article/{item.get('source')}/{item.get('id')}",
            "pdf_url": pdf,
            "oa_status": "oa" if pdf else None,
            "citation_count": item.get("citedByCount"),
            "metadata": dict(item),
        }

    @staticmethod
    def _hal_item(item: Mapping[str, Any]) -> dict[str, Any]:
        pdf = _first(item.get("fileMain_s") or item.get("fileAnnexes_s")) or None
        return {
            "source": "hal",
            "title": _first(item.get("title_s")),
            "abstract": _first(item.get("abstract_s")),
            "authors": _authors(item.get("authFullName_s")),
            "year": item.get("producedDateY_i"),
            "venue": _text(item.get("journalTitle_s")),
            "doi": _doi(item.get("doiId_s")),
            "url": _text(item.get("uri_s")) or None,
            "pdf_url": pdf,
            "oa_status": "oa" if pdf else None,
            "metadata": dict(item),
        }

    @staticmethod
    def _core_item(item: Mapping[str, Any]) -> dict[str, Any]:
        pdf = _text(item.get("downloadUrl")) or None
        return {
            "source": "core",
            "title": _text(item.get("title")),
            "abstract": _text(item.get("abstract")),
            "authors": _authors(item.get("authors")),
            "year": item.get("yearPublished") or _year(item.get("publishedDate")),
            "venue": _text(item.get("publisher") or item.get("journals")),
            "doi": _doi(item.get("doi")),
            "url": _text(item.get("sourceFulltextUrls")) or None,
            "pdf_url": pdf,
            "oa_status": "oa" if pdf else None,
            "citation_count": item.get("citationCount"),
            "metadata": dict(item),
        }

    @staticmethod
    def _base_item(item: Mapping[str, Any]) -> dict[str, Any]:
        url = _first(item.get("dclink") or item.get("dcidentifier")) or None
        return {
            "source": "base",
            "title": _first(item.get("dctitle")),
            "abstract": _first(item.get("dcdescription")),
            "authors": _authors(item.get("dccreator")),
            "year": _year(item.get("dcyear") or item.get("dcdate")),
            "venue": _first(item.get("dcsource") or item.get("dcpublisher")),
            "doi": _doi(_first(item.get("dcdoi") or item.get("doi"))),
            "url": url,
            "metadata": dict(item),
        }

    @staticmethod
    def _sciverse_item(item: Mapping[str, Any]) -> dict[str, Any]:
        pdf = _text(item.get("pdf_url") or item.get("access_oa_url")) or None
        return {
            "source": "sciverse",
            "title": _text(item.get("title") or item.get("display_name")),
            "abstract": _text(item.get("abstract")),
            "authors": _authors(item.get("authors") or item.get("author")),
            "year": item.get("year") or item.get("publication_published_year"),
            "venue": _text(item.get("venue") or item.get("publication_venue_name_unified")),
            "doi": _doi(item.get("doi")),
            "url": _text(item.get("url") or item.get("access_oa_url")) or None,
            "pdf_url": pdf,
            "oa_status": "oa" if pdf else None,
            "citation_count": item.get("citation_count"),
            "metadata": dict(item),
        }
