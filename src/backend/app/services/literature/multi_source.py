"""Small async clients for the public providers used by the discovery runtime.

The provider payloads are intentionally normalized here instead of leaking
source-specific JSON into the worker.  Unpaywall is an OA resolver (DOI
lookup), so its keyword-search method returns no candidates by design.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import get_settings


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
    match = re.search(r"\b(?:19|20)\d{2}\b", _text(value))
    return int(match.group(0)) if match else None


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


class MultiSourceClient:
    """HTTP client for the non-core providers in the YFR-compatible source set."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._client = client or httpx.AsyncClient(
            proxy=settings.outbound_proxy or None,
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": f"Polaris/1.0 (mailto:{settings.openalex_mailto})"},
        )
        self._owns_client = client is None

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
        return await handler(request) if handler else []

    async def lookup_unpaywall(self, doi: str) -> dict[str, Any] | None:
        settings = get_settings()
        if not settings.unpaywall_email or not doi:
            return None
        try:
            response = await self._client.get(
                f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}",
                params={"email": settings.unpaywall_email},
                timeout=15.0,
            )
            if response.status_code >= 400:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _search_pubmed(self, request: Any) -> list[dict[str, Any]]:
        settings = get_settings()
        start, end = _date_window(request)
        query = request.query
        if start or end:
            lo = start or 1800
            hi = end or datetime.now(UTC).year
            query = f"({query}) AND ({lo}:{hi}[pdat])"
        params = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": min(request.limit, 1000),
            "sort": "relevance",
        }
        if settings.pubmed_api_key:
            params["api_key"] = settings.pubmed_api_key
        if settings.pubmed_email:
            params["email"] = settings.pubmed_email
        try:
            response = await self._client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=params
            )
            response.raise_for_status()
            ids = (response.json().get("esearchresult") or {}).get("idlist") or []
            if not ids:
                return []
            summary_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
            if settings.pubmed_api_key:
                summary_params["api_key"] = settings.pubmed_api_key
            summary = await self._client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params=summary_params,
            )
            summary.raise_for_status()
            result = summary.json().get("result") or {}
        except (httpx.HTTPError, ValueError):
            return []
        rows: list[dict[str, Any]] = []
        for pmid in ids:
            item = result.get(str(pmid))
            if not isinstance(item, dict) or not item.get("title"):
                continue
            article_ids = {
                str(article.get("idtype")): article.get("value")
                for article in item.get("articleids") or []
                if isinstance(article, dict)
            }
            rows.append(
                {
                    "source": "pubmed",
                    "pmid": str(pmid),
                    "title": _text(item.get("title")),
                    "abstract": None,
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
        filters = ["type:journal-article"]
        if request.start_year:
            filters.append(f"from-pub-date:{request.start_year}-01-01")
        if request.end_year:
            filters.append(f"until-pub-date:{request.end_year}-12-31")
        params = {
            "query": request.query,
            "rows": min(request.limit, 1000),
            "filter": ",".join(filters),
            "sort": "relevance",
            "order": "desc",
        }
        headers = {"User-Agent": f"Polaris/1.0 (mailto:{settings.crossref_mailto})"}
        try:
            response = await self._client.get(
                "https://api.crossref.org/works", params=params, headers=headers
            )
            response.raise_for_status()
            items = (response.json().get("message") or {}).get("items") or []
        except (httpx.HTTPError, ValueError):
            return []
        return [self._crossref_item(item) for item in items if isinstance(item, dict)]

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
            response = await self._client.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search", params=params
            )
            response.raise_for_status()
            items = (response.json().get("resultList") or {}).get("result") or []
        except (httpx.HTTPError, ValueError):
            return []
        return [self._europepmc_item(item) for item in items if isinstance(item, dict)]

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
            response = await self._client.get(
                "https://api.archives-ouvertes.fr/search/", params=params
            )
            response.raise_for_status()
            items = (response.json().get("response") or {}).get("docs") or []
        except (httpx.HTTPError, ValueError):
            return []
        return [self._hal_item(item) for item in items if isinstance(item, dict)]

    async def _search_core(self, request: Any) -> list[dict[str, Any]]:
        settings = get_settings()
        if not settings.core_api_key:
            return []
        params = {
            "q": f"{request.query} yearPublished>={request.start_year or 1800}",
            "limit": min(request.limit, 100),
        }
        try:
            response = await self._client.post(
                "https://api.core.ac.uk/v3/search/works",
                json=params,
                headers={"Authorization": f"Bearer {settings.core_api_key}"},
            )
            response.raise_for_status()
            items = response.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            return []
        return [self._core_item(item) for item in items if isinstance(item, dict)]

    async def _search_base(self, request: Any) -> list[dict[str, Any]]:
        start, end = _date_window(request)
        params = {
            "func": "PerformSearch",
            "query": f"{request.query} year:{start or 1800}-{end or datetime.now(UTC).year}",
            "format": "json",
            "hits": min(request.limit, 100),
        }
        try:
            response = await self._client.get(
                "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
            items = (payload.get("response") or {}).get("docs") or payload.get("docs") or []
        except (httpx.HTTPError, ValueError):
            return []
        return [self._base_item(item) for item in items if isinstance(item, dict)]

    async def _search_sciverse(self, request: Any) -> list[dict[str, Any]]:
        settings = get_settings()
        token = next(
            (
                item.strip()
                for item in re.split(r"[,;\s]+", settings.sciverse_api_tokens)
                if item.strip()
            ),
            "",
        )
        if not token:
            return []
        try:
            response = await self._client.post(
                f"{settings.sciverse_base_url.rstrip('/')}/meta-search",
                json={
                    "query": request.query,
                    "page_size": min(request.limit, 100),
                    "collection": "papers",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        items = payload.get("results") or payload.get("items") or payload.get("data") or []
        return [self._sciverse_item(item) for item in items if isinstance(item, dict)]

    @staticmethod
    def _crossref_item(item: Mapping[str, Any]) -> dict[str, Any]:
        date_parts = (
            item.get("published-print") or item.get("published-online") or item.get("issued") or {}
        ).get("date-parts") or []
        year = date_parts[0][0] if date_parts and date_parts[0] else _year(item.get("created"))
        authors = []
        for author in item.get("author") or []:
            name = _text(
                author.get("name")
                or " ".join(filter(None, [author.get("given"), author.get("family")]))
            )
            if name:
                authors.append({"name": name})
        pdf_url = next(
            (
                _text(link.get("URL"))
                for link in item.get("link") or []
                if isinstance(link, dict) and "pdf" in _text(link.get("content-type")).lower()
            ),
            None,
        )
        return {
            "source": "crossref",
            "title": _first(item.get("title")),
            "abstract": _text(item.get("abstract")),
            "authors": authors,
            "year": year,
            "venue": _first(item.get("container-title")),
            "doi": _doi(item.get("DOI")),
            "url": item.get("URL"),
            "pdf_url": pdf_url,
            "oa_status": "oa" if pdf_url else None,
            "citation_count": item.get("is-referenced-by-count"),
            "metadata": dict(item),
        }

    @staticmethod
    def _europepmc_item(item: Mapping[str, Any]) -> dict[str, Any]:
        links = (item.get("fullTextUrlList") or {}).get("fullTextUrl") or []
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
