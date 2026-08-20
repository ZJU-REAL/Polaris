"""Provider-neutral contracts for academic discovery and metadata import."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

SearchSort = Literal["relevance", "newest", "citation"]
PaginationType = Literal["offset", "cursor", "token", "history", "none"]
ImportKind = Literal["arxiv", "doi", "corpus_id", "bibtex"]


@dataclass(frozen=True, slots=True)
class Identifier:
    namespace: str
    value: str


@dataclass(frozen=True, slots=True)
class ImportInput:
    """One manual import item; legacy API fields are converted to this shape."""

    kind: ImportKind
    value: str


@dataclass(frozen=True, slots=True)
class ProviderRelation:
    relation_type: str
    target: Identifier


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """Canonical metadata returned by a literature provider.

    ``raw_metadata`` is retained only for diagnostics and provider-specific
    enrichment. ORM writes must use the normalized fields and identifiers.
    """

    source: str
    source_record_id: str
    title: str
    identifiers: tuple[Identifier, ...] = ()
    authors: tuple[dict[str, Any], ...] = ()
    affiliations: tuple[str, ...] = ()
    abstract: str | None = None
    year: int | None = None
    venue: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    relations: tuple[ProviderRelation, ...] = ()
    raw_metadata: dict[str, Any] | None = field(default=None, compare=False, repr=False)

    def identifier(self, namespace: str) -> str | None:
        wanted = namespace.casefold()
        return next(
            (
                identifier.value
                for identifier in self.identifiers
                if identifier.namespace.casefold() == wanted
            ),
            None,
        )

    def to_paper_fields(self) -> dict[str, Any]:
        external_ids = {
            identifier.namespace: identifier.value for identifier in self.identifiers
        }
        return {
            "source": self.source,
            "title": self.title,
            "authors": list(self.authors) or None,
            "affiliations": list(self.affiliations),
            "abstract": self.abstract,
            "year": self.year,
            "venue": self.venue,
            "url": self.url,
            "published_at": self.published_at,
            "arxiv_id": self.identifier("arxiv"),
            "doi": self.identifier("doi"),
            "external_ids": external_ids or None,
        }


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str = ""
    date_from: datetime | None = None
    date_to: datetime | None = None
    sort: SearchSort = "relevance"
    limit: int = 50
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchPage:
    records: tuple[ProviderRecord, ...]
    source: str
    next_cursor: str | None = None
    unsupported_filters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    search: bool = False
    metadata: bool = False
    references: bool = False
    citations: bool = False
    fulltext_hints: bool = False
    supported_filters: frozenset[str] = frozenset()
    pagination_type: PaginationType = "none"


@runtime_checkable
class SearchProvider(Protocol):
    provider_id: str
    capabilities: ProviderCapabilities

    async def search(self, query: SearchQuery, cursor: str | None = None) -> SearchPage: ...


@runtime_checkable
class MetadataProvider(Protocol):
    provider_id: str
    capabilities: ProviderCapabilities

    async def get_metadata(self, identifiers: list[Identifier]) -> list[ProviderRecord]: ...


@runtime_checkable
class CitationProvider(Protocol):
    provider_id: str
    capabilities: ProviderCapabilities

    async def get_references(self, identifier: Identifier) -> list[ProviderRecord]: ...

    async def get_citations(self, identifier: Identifier) -> list[ProviderRecord]: ...
