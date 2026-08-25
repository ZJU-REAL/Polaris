"""Adapters from concrete literature clients to provider-neutral contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.services.literature.arxiv import ArxivClient, normalize_arxiv_id
from app.services.literature.contracts import (
    Identifier,
    ProviderCapabilities,
    ProviderRecord,
    ProviderRelation,
    SearchPage,
    SearchQuery,
)
from app.services.literature.crossref import CrossrefClient
from app.services.literature.openalex import OpenAlexClient
from app.services.literature.semantic_scholar import SemanticScholarClient

_CURSOR_RE = re.compile(r"^[0-9]+$")


def _unsupported_query_features(
    query: SearchQuery,
    capabilities: ProviderCapabilities,
    *,
    supports_dates: bool = False,
    supported_sorts: frozenset[str] = frozenset({"relevance"}),
) -> tuple[str, ...]:
    unsupported = set(query.filters) - capabilities.supported_filters
    if not supports_dates:
        if query.date_from is not None:
            unsupported.add("date_from")
        if query.date_to is not None:
            unsupported.add("date_to")
    if query.sort not in supported_sorts:
        unsupported.add("sort")
    return tuple(sorted(unsupported))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _authors(values: Any) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for value in values or []:
        if isinstance(value, str) and value.strip():
            result.append({"name": value.strip()})
        elif isinstance(value, dict) and value.get("name"):
            result.append(dict(value))
    return tuple(result)


def _identifiers(values: dict[str, Any]) -> tuple[Identifier, ...]:
    result: list[Identifier] = []
    for namespace, value in values.items():
        if value is None or not str(value).strip():
            continue
        result.append(Identifier(namespace=namespace.casefold(), value=str(value).strip()))
    return tuple(result)


def arxiv_record(entry: dict[str, Any]) -> ProviderRecord:
    arxiv_id = normalize_arxiv_id(str(entry.get("arxiv_id") or ""))
    identifiers = {"arxiv": arxiv_id}
    if entry.get("doi"):
        identifiers["doi"] = entry["doi"]
    return ProviderRecord(
        source="arxiv",
        source_record_id=arxiv_id,
        title=str(entry.get("title") or "").strip(),
        identifiers=_identifiers(identifiers),
        authors=_authors(entry.get("authors")),
        abstract=entry.get("abstract"),
        year=entry.get("year"),
        venue=entry.get("primary_category"),
        url=entry.get("url"),
        published_at=_parse_datetime(entry.get("published")),
        raw_metadata=entry,
    )


def openalex_record(work: dict[str, Any]) -> ProviderRecord:
    openalex_id = str(work.get("openalex_id") or "").rsplit("/", 1)[-1]
    identifiers: dict[str, Any] = {"openalex": openalex_id}
    if work.get("doi"):
        identifiers["doi"] = work["doi"]
    return ProviderRecord(
        source="openalex",
        source_record_id=openalex_id,
        title=str(work.get("title") or "").strip(),
        identifiers=_identifiers(identifiers),
        authors=_authors(work.get("authors")),
        affiliations=tuple(str(value) for value in work.get("affiliations") or [] if value),
        abstract=work.get("abstract"),
        year=work.get("year"),
        venue=work.get("venue"),
        url=work.get("url"),
        published_at=_parse_datetime(work.get("published")),
        raw_metadata=work,
    )


def semantic_scholar_record(paper: dict[str, Any]) -> ProviderRecord:
    external = paper.get("externalIds") or {}
    identifiers: dict[str, Any] = {"s2": paper.get("paperId")}
    key_map = {"ArXiv": "arxiv", "DOI": "doi", "PubMed": "pmid", "CorpusId": "s2_corpus"}
    for source_key, namespace in key_map.items():
        if external.get(source_key) is not None:
            identifiers[namespace] = external[source_key]
    authors = tuple(
        {"name": author["name"]}
        for author in paper.get("authors") or []
        if isinstance(author, dict) and author.get("name")
    )
    return ProviderRecord(
        source="semantic_scholar",
        source_record_id=str(paper.get("paperId") or ""),
        title=str(paper.get("title") or "").strip(),
        identifiers=_identifiers(identifiers),
        authors=authors,
        abstract=paper.get("abstract"),
        year=paper.get("year"),
        venue=paper.get("venue"),
        url=paper.get("url"),
        published_at=_parse_datetime(paper.get("publicationDate")),
        raw_metadata=paper,
    )


def _crossref_date(message: dict[str, Any]) -> datetime | None:
    for key in ("published-print", "published-online", "published", "issued"):
        parts = ((message.get(key) or {}).get("date-parts") or [[]])[0]
        if not parts:
            continue
        try:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return datetime(year, month, day, tzinfo=UTC)
        except (TypeError, ValueError):
            continue
    return None


def crossref_record(message: dict[str, Any]) -> ProviderRecord:
    doi = str(message.get("DOI") or "").strip()
    authors = tuple(
        {
            "name": " ".join(
                part
                for part in (
                    str(author.get("given") or ""),
                    str(author.get("family") or ""),
                )
                if part
            )
        }
        for author in message.get("author") or []
        if isinstance(author, dict) and (author.get("given") or author.get("family"))
    )
    relations: list[ProviderRelation] = []
    for relation_type, targets in (message.get("relation") or {}).items():
        for target in targets or []:
            if not isinstance(target, dict) or not target.get("id"):
                continue
            namespace = str(target.get("id-type") or "doi").casefold()
            relations.append(
                ProviderRelation(
                    relation_type=str(relation_type),
                    target=Identifier(namespace=namespace, value=str(target["id"])),
                )
            )
    published_at = _crossref_date(message)
    titles = message.get("title") or []
    containers = message.get("container-title") or []
    return ProviderRecord(
        source="crossref",
        source_record_id=doi,
        title=str(titles[0] if titles else "").strip(),
        identifiers=_identifiers({"doi": doi}),
        authors=authors,
        abstract=message.get("abstract"),
        year=published_at.year if published_at else None,
        venue=str(containers[0]) if containers else None,
        url=message.get("URL") or (f"https://doi.org/{doi}" if doi else None),
        published_at=published_at,
        relations=tuple(relations),
        raw_metadata=message,
    )


class ArxivSearchProvider:
    provider_id = "arxiv"
    capabilities = ProviderCapabilities(
        search=True,
        metadata=True,
        fulltext_hints=True,
        supported_filters=frozenset({"categories", "exclude", "keywords"}),
        pagination_type="offset",
    )

    def __init__(self, client: ArxivClient) -> None:
        self._client = client

    async def search(self, query: SearchQuery, cursor: str | None = None) -> SearchPage:
        start = int(cursor or 0) if _CURSOR_RE.fullmatch(cursor or "0") else 0
        filters = query.filters
        page_size = min(self._client.page_size, max(1, query.limit))
        entries = await self._client.search_page(
            categories=list(filters.get("categories") or []),
            keywords=list(filters.get("keywords") or ([query.text] if query.text else [])),
            exclude=list(filters.get("exclude") or []),
            since=query.date_from,
            until=query.date_to,
            start=start,
            limit=page_size,
        )
        next_cursor = str(start + len(entries)) if len(entries) == page_size else None
        unsupported = _unsupported_query_features(
            query,
            self.capabilities,
            supports_dates=True,
            supported_sorts=frozenset({"newest"}),
        )
        return SearchPage(
            records=tuple(arxiv_record(entry) for entry in entries if entry.get("title")),
            source=self.provider_id,
            next_cursor=next_cursor,
            unsupported_filters=unsupported,
        )

    async def get_metadata(self, identifiers: list[Identifier]) -> list[ProviderRecord]:
        arxiv_ids = [
            identifier.value for identifier in identifiers if identifier.namespace == "arxiv"
        ]
        entries = await self._client.fetch_by_ids(arxiv_ids)
        return [arxiv_record(entry) for entry in entries if entry.get("title")]


class OpenAlexProvider:
    provider_id = "openalex"
    capabilities = ProviderCapabilities(search=True, metadata=True, pagination_type="none")

    def __init__(self, client: OpenAlexClient) -> None:
        self._client = client

    async def search(self, query: SearchQuery, cursor: str | None = None) -> SearchPage:
        works = await self._client.search_works(query.text, limit=query.limit)
        unsupported = _unsupported_query_features(query, self.capabilities)
        return SearchPage(
            records=tuple(openalex_record(work) for work in works if work.get("title")),
            source=self.provider_id,
            unsupported_filters=unsupported,
        )

    async def get_metadata(self, identifiers: list[Identifier]) -> list[ProviderRecord]:
        records: list[ProviderRecord] = []
        for identifier in identifiers:
            work = None
            if identifier.namespace == "doi":
                work = await self._client.get_by_doi(identifier.value)
            elif identifier.namespace == "arxiv":
                work = await self._client.get_by_arxiv(identifier.value)
            elif identifier.namespace == "openalex":
                work = await self._client.get_by_id(identifier.value)
            if work and work.get("title"):
                records.append(openalex_record(work))
        return records


class SemanticScholarProvider:
    provider_id = "semantic_scholar"
    capabilities = ProviderCapabilities(
        search=True, metadata=True, references=True, citations=True, pagination_type="none"
    )

    def __init__(self, client: SemanticScholarClient) -> None:
        self._client = client

    @staticmethod
    def _paper_id(identifier: Identifier) -> str:
        prefixes = {
            "arxiv": "arXiv",
            "doi": "DOI",
            "pmid": "PMID",
            "s2_corpus": "CorpusId",
        }
        prefix = prefixes.get(identifier.namespace)
        return f"{prefix}:{identifier.value}" if prefix else identifier.value

    async def search(self, query: SearchQuery, cursor: str | None = None) -> SearchPage:
        papers = await self._client.search_papers(query.text, limit=query.limit)
        return SearchPage(
            records=tuple(semantic_scholar_record(paper) for paper in papers if paper.get("title")),
            source=self.provider_id,
            unsupported_filters=_unsupported_query_features(query, self.capabilities),
        )

    async def get_metadata(self, identifiers: list[Identifier]) -> list[ProviderRecord]:
        records = []
        for identifier in identifiers:
            paper = await self._client.get_paper(self._paper_id(identifier))
            if paper.get("title"):
                records.append(semantic_scholar_record(paper))
        return records

    async def get_references(self, identifier: Identifier) -> list[ProviderRecord]:
        papers = await self._client.get_references(self._paper_id(identifier))
        return [semantic_scholar_record(paper) for paper in papers if paper.get("title")]

    async def get_citations(self, identifier: Identifier) -> list[ProviderRecord]:
        papers = await self._client.get_citations(self._paper_id(identifier))
        return [semantic_scholar_record(paper) for paper in papers if paper.get("title")]


class CrossrefProvider:
    provider_id = "crossref"
    capabilities = ProviderCapabilities(search=True, metadata=True, pagination_type="cursor")

    def __init__(self, client: CrossrefClient) -> None:
        self._client = client

    async def search(self, query: SearchQuery, cursor: str | None = None) -> SearchPage:
        works, next_cursor = await self._client.search_works_page(
            query.text, limit=query.limit, cursor=cursor
        )
        return SearchPage(
            records=tuple(crossref_record(work) for work in works if work.get("title")),
            source=self.provider_id,
            next_cursor=next_cursor,
            unsupported_filters=_unsupported_query_features(query, self.capabilities),
        )

    async def get_metadata(self, identifiers: list[Identifier]) -> list[ProviderRecord]:
        records = []
        for identifier in identifiers:
            if identifier.namespace != "doi":
                continue
            work = await self._client.get_work(identifier.value)
            if work and work.get("title"):
                records.append(crossref_record(work))
        return records
