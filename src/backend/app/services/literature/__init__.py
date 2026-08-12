"""Literature clients and provider adapters.

模块级单例经工厂函数获取；测试用 ``set_clients`` 注入
（fakeredis + min_interval=0 + respx mock 的 httpx client）。
"""

from app.services.literature.arxiv import ArxivClient
from app.services.literature.crossref import CrossrefClient
from app.services.literature.openalex import OpenAlexClient
from app.services.literature.providers import (
    ArxivSearchProvider,
    CrossrefProvider,
    OpenAlexProvider,
    SemanticScholarProvider,
)
from app.services.literature.semantic_scholar import SemanticScholarClient

_arxiv: ArxivClient | None = None
_s2: SemanticScholarClient | None = None
_openalex: OpenAlexClient | None = None
_crossref: CrossrefClient | None = None


def get_arxiv_client() -> ArxivClient:
    global _arxiv
    if _arxiv is None:
        _arxiv = ArxivClient()
    return _arxiv


def get_s2_client() -> SemanticScholarClient:
    global _s2
    if _s2 is None:
        _s2 = SemanticScholarClient()
    return _s2


def get_openalex_client() -> OpenAlexClient:
    global _openalex
    if _openalex is None:
        _openalex = OpenAlexClient()
    return _openalex


def get_crossref_client() -> CrossrefClient:
    global _crossref
    if _crossref is None:
        _crossref = CrossrefClient()
    return _crossref


def get_search_providers() -> dict[str, object]:
    """Return adapters without changing the concrete-client compatibility API."""
    return {
        "arxiv": ArxivSearchProvider(get_arxiv_client()),
        "semantic_scholar": SemanticScholarProvider(get_s2_client()),
        "openalex": OpenAlexProvider(get_openalex_client()),
        "crossref": CrossrefProvider(get_crossref_client()),
    }


def set_clients(
    *,
    arxiv: ArxivClient | None = None,
    s2: SemanticScholarClient | None = None,
    openalex: OpenAlexClient | None = None,
    crossref: CrossrefClient | None = None,
) -> None:
    """测试注入用；传 None 的槽位不动。"""
    global _arxiv, _s2, _openalex, _crossref
    if arxiv is not None:
        _arxiv = arxiv
    if s2 is not None:
        _s2 = s2
    if openalex is not None:
        _openalex = openalex
    if crossref is not None:
        _crossref = crossref


def reset_clients() -> None:
    global _arxiv, _s2, _openalex, _crossref
    _arxiv = None
    _s2 = None
    _openalex = None
    _crossref = None


__all__ = [
    "ArxivClient",
    "CrossrefClient",
    "OpenAlexClient",
    "SemanticScholarClient",
    "get_arxiv_client",
    "get_crossref_client",
    "get_openalex_client",
    "get_s2_client",
    "get_search_providers",
    "reset_clients",
    "set_clients",
]
