"""文献源注册表：源 id → 适配器，全后端唯一的源分派点（#720）。

以前有三套并行的「源清单」：discovery 的 AdapterRegistry 硬编码构造、
literature_settings.SUPPORTED_SOURCES 手写元组、以及 12 处直接 import
客户端的旁路。三套各自演化，新增一个源要改三处，漏一处就出现
「设置里配得上、检索里用不到」的裂缝。本模块收拢成一张表：

- ``register_source`` 是与 extraction 的 register_schema / runners 的
  register 同款的注册缝：内置源在模块底部注册，学科包/插件后续挂新源
  也走这一道，不再改任何清单常量。
- literature_settings 的可选源清单（supported_sources()）与 discovery 的
  AdapterRegistry 构造都从这张表派生，顺序 = 注册顺序。
- 旁路调用点改为 ``get_source()/require_source()`` 取适配器再调能力方法；
  适配器内部包的仍是 ``app.services.literature`` 的模块级单例客户端，
  行为（限速状态、缓存、测试注入缝 set_clients）与直连时代逐字节一致。

能力模型：``search`` 是唯一必选能力；``fetch_new`` / ``resolve`` /
``download_pdf`` / ``snowball`` 等按源实现，调用方用 ``hasattr`` 探测
（见 schemas/literature_discovery.SourceAdapter 的说明）。解析级联
（arXiv → OpenAlex → S2）的顺序也是注册元数据（resolve_kinds +
resolve_priority），不再散落在 paper_import 的函数排布里。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from app.core.config import get_settings
from app.schemas.literature_discovery import (
    LiteratureCandidate,
    SourceSearchPage,
    SourceSearchRequest,
)
from app.services.literature.discovery import validate_candidate
from app.services.literature.multi_source import MultiSourceClient

# ---- 适配器实现（原 runtime.py 的内置适配器，随注册表一起收拢到这里） ----

_ROTATION_LOCK = Lock()
_ROTATION_INDEX: dict[str, int] = {}


class RotatingAdapter:
    """Select one configured credential without persisting it in a run."""

    def __init__(self, name: str, adapters: list[Any] | tuple[Any, ...]) -> None:
        if not adapters:
            raise ValueError("at least one adapter is required")
        self.name = name
        self._adapters = tuple(adapters)

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        with _ROTATION_LOCK:
            index = _ROTATION_INDEX.get(self.name, 0)
            _ROTATION_INDEX[self.name] = index + 1
        return await self._adapters[index % len(self._adapters)].search(request)


class OpenAlexAdapter:
    name = "openalex"
    #: 能按哪些标识解析元数据（capability：resolve）
    resolve_kinds = ("arxiv", "doi")

    def __init__(self, client: Any) -> None:
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        rows = await self.client.search_works(
            request.query,
            limit=request.limit,
            start_year=request.start_year,
            end_year=request.end_year,
        )
        return SourceSearchPage(
            source=self.name,
            items=[_candidate_from_openalex(row) for row in rows],
            fetched_count=len(rows),
        )

    # —— 可选能力：均为对客户端的薄委托，不做任何字段加工（字节等价要求） ——

    async def resolve(self, kind: str, value: str) -> dict[str, Any] | None:
        """按外部标识取元数据记录；查不到返回 None。"""
        if kind == "arxiv":
            return await self.client.get_by_arxiv(value)
        if kind == "doi":
            return await self.client.get_by_doi(value)
        raise ValueError(f"openalex cannot resolve identifier kind: {kind}")

    async def search_works(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.client.search_works(query, **kwargs)


class SemanticScholarAdapter:
    name = "semantic"
    resolve_kinds = ("corpus_id",)

    def __init__(self, client: Any) -> None:
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        rows = await self.client.search_papers(
            request.query,
            limit=request.limit,
            start_year=request.start_year,
            end_year=request.end_year,
        )
        return SourceSearchPage(
            source=self.name,
            items=[_candidate_from_semantic(row) for row in rows],
            fetched_count=len(rows),
        )

    # —— 可选能力 ——

    async def resolve(self, kind: str, value: str) -> dict[str, Any] | None:
        if kind == "corpus_id":
            return await self.client.get_paper(f"CorpusId:{value}")
        raise ValueError(f"semantic scholar cannot resolve identifier kind: {kind}")

    async def snowball(self, paper_ref: str) -> list[dict[str, Any]]:
        """引文扩展：参考文献 + 施引文献，一次种子一份合并清单。

        顺序固定为「参考文献在前、施引在后」——与旁路时代 ``refs + cits``
        的拼接顺序一致，去重责任在调用方（保持原有行为）。
        """
        refs = await self.client.get_references(paper_ref)
        cits = await self.client.get_citations(paper_ref)
        return refs + cits

    async def search_papers(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.client.search_papers(query, **kwargs)

    async def get_references(self, paper_ref: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.client.get_references(paper_ref, **kwargs)

    async def get_citations(self, paper_ref: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.client.get_citations(paper_ref, **kwargs)

    async def get_paper(self, paper_ref: str, **kwargs: Any) -> dict[str, Any]:
        return await self.client.get_paper(paper_ref, **kwargs)


class ArxivAdapter:
    name = "arxiv"
    resolve_kinds = ("arxiv",)

    def __init__(self, client: Any) -> None:
        self.client = client

    @property
    def page_size(self) -> int:
        """arXiv 分页大小由客户端决定（调用方据此判断末页，不能写死）。"""
        return self.client.page_size

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        since = datetime(request.start_year, 1, 1, tzinfo=UTC) if request.start_year else None
        until = datetime(request.end_year, 12, 31, 23, 59, tzinfo=UTC) if request.end_year else None
        if hasattr(self.client, "search_raw"):
            rows = await self.client.search_raw(
                request.query, since=since, until=until, limit=request.limit
            )
        else:
            rows = await self.client.search(
                keywords=[request.query], since=since, until=until, limit=request.limit
            )
        return SourceSearchPage(
            source=self.name,
            items=[_candidate_from_arxiv(row) for row in rows],
            fetched_count=len(rows),
        )

    # —— 可选能力 ——

    async def fetch_new(self, category: str) -> tuple[list[dict[str, Any]], datetime | None]:
        """当天新公告列表（每日池的增量供给）。"""
        return await self.client.fetch_new(category)

    async def resolve(self, kind: str, value: str) -> dict[str, Any] | None:
        if kind == "arxiv":
            entries = await self.client.fetch_by_ids([value])
            return next((e for e in entries if e.get("title")), None)
        raise ValueError(f"arxiv cannot resolve identifier kind: {kind}")

    async def fetch_by_ids(self, arxiv_ids: list[str]) -> list[dict[str, Any]]:
        return await self.client.fetch_by_ids(arxiv_ids)

    async def download_pdf(self, arxiv_id: str) -> bytes:
        return await self.client.download_pdf(arxiv_id)

    async def search_page(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.client.search_page(**kwargs)


class MultiSourceAdapter:
    """Adapter for providers that share the normalized YFR-compatible client."""

    def __init__(self, name: str, client: MultiSourceClient) -> None:
        self.name = name
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        rows = await self.client.search_source(self.name, request)
        return SourceSearchPage(
            source=self.name,
            items=[validate_candidate(_candidate_from_generic(self.name, row)) for row in rows],
            fetched_count=len(rows),
        )


def _candidate_from_openalex(row: Mapping[str, Any]) -> LiteratureCandidate:
    return validate_candidate(
        LiteratureCandidate(
            source="openalex",
            title=str(row.get("title") or "Untitled"),
            abstract=row.get("abstract"),
            authors=row.get("authors") or [],
            year=row.get("year"),
            venue=row.get("venue"),
            doi=row.get("doi"),
            url=row.get("url"),
            citation_count=row.get("cited_by_count"),
            metadata=dict(row),
        )
    )


def _candidate_from_semantic(row: Mapping[str, Any]) -> LiteratureCandidate:
    external = row.get("externalIds") or {}
    return validate_candidate(
        LiteratureCandidate(
            source="semantic",
            title=str(row.get("title") or "Untitled"),
            abstract=row.get("abstract"),
            authors=[a for a in row.get("authors") or [] if isinstance(a, Mapping)],
            year=row.get("year"),
            venue=row.get("venue"),
            doi=external.get("DOI"),
            arxiv_id=external.get("ArXiv"),
            semantic_scholar_id=row.get("paperId"),
            url=row.get("url"),
            citation_count=row.get("citationCount"),
            metadata=dict(row),
        )
    )


def _candidate_from_arxiv(row: Mapping[str, Any]) -> LiteratureCandidate:
    return validate_candidate(
        LiteratureCandidate(
            source="arxiv",
            title=str(row.get("title") or "Untitled"),
            abstract=row.get("abstract"),
            authors=row.get("authors") or [],
            year=row.get("year"),
            doi=row.get("doi"),
            arxiv_id=row.get("arxiv_id"),
            url=row.get("url"),
            pdf_url=row.get("pdf_url"),
            oa_status="oa" if row.get("pdf_url") else None,
            metadata=dict(row),
        )
    )


def _candidate_from_generic(source: str, row: Mapping[str, Any]) -> LiteratureCandidate:
    return LiteratureCandidate(
        source=source,
        title=str(row.get("title") or "Untitled"),
        abstract=row.get("abstract"),
        authors=row.get("authors") or [],
        year=row.get("year"),
        venue=row.get("venue"),
        doi=row.get("doi"),
        pmid=row.get("pmid"),
        url=row.get("url"),
        pdf_url=row.get("pdf_url"),
        oa_status=row.get("oa_status"),
        citation_count=row.get("citation_count"),
        metadata=dict(row.get("metadata") or row),
    )


# ---- 注册表本体 ----


def credential_pool(settings: Mapping[str, Any], source: str, fallback: str = "") -> list[str]:
    configured = settings.get("provider_keys")
    # 「这个源没配」和「配了但空（= 管理员停用了）」必须分开：只看池子空不空的话，
    # 停用等于无效——照样回落到环境变量里的凭据，而且没有任何提示。
    declared = isinstance(configured, Mapping) and source in configured
    values = configured.get(source) if declared else None
    pool = [str(value).strip() for value in values or [] if str(value).strip()]
    if declared:
        return pool
    return [item for value in fallback.replace(";", ",").split(",") if (item := value.strip())]


@dataclass(frozen=True)
class SourceBuildContext:
    """凭据化构建的输入：管理员运行时设置 + 应用设置 + 共享的多源客户端。"""

    runtime_settings: Mapping[str, Any]
    app_settings: Any
    multi_source: MultiSourceClient

    def credential_pool(self, source: str, fallback: str = "") -> list[str]:
        return credential_pool(self.runtime_settings, source, fallback)


@dataclass(frozen=True)
class SourceSpec:
    """一个文献源的注册信息。

    - ``build``：凭据化构建（discovery 检索用）——从管理员设置里取 key 池，
      可能包成 RotatingAdapter 做 key 轮转。
    - ``default_factory``：免凭据构建（其余链路用）——包模块级单例客户端，
      保住单例的限速状态 / 缓存 / set_clients 测试注入缝；``client`` 参数
      允许调用方显式给客户端（各调用模块保留自己的 get_*_client 注入缝）。
    - ``selectable``：是否出现在管理设置的可选源列表（unpaywall 只做 OA
      解析，不参与检索选择，与收拢前口径一致）。
    - ``resolve_kinds`` + ``resolve_priority``：解析级联元数据；
      ``resolvers_for(kind)`` 按 priority 升序给出级联顺序。
    """

    id: str
    build: Callable[[SourceBuildContext], Any]
    default_factory: Callable[[Any | None], Any] | None = None
    selectable: bool = True
    resolve_kinds: tuple[str, ...] = ()
    resolve_priority: int = 100


_SPECS: dict[str, SourceSpec] = {}
_REGISTER_LOCK = Lock()


def register_source(spec: SourceSpec) -> None:
    """注册一个文献源。重名直接拒绝——静默覆盖会把源分派变成 import 顺序问题。"""
    key = spec.id.strip().lower()
    if not key:
        raise ValueError("literature source id must be non-empty")
    with _REGISTER_LOCK:
        if key in _SPECS:
            raise ValueError(f"literature source already registered: {key!r}")
        _SPECS[key] = spec


def unregister_source(source_id: str) -> None:
    """移除注册（插件卸载 / 测试清理用）；未注册时静默返回。"""
    with _REGISTER_LOCK:
        _SPECS.pop(source_id.strip().lower(), None)


def specs() -> tuple[SourceSpec, ...]:
    """全部注册源，按注册顺序（dict 插入序）。"""
    return tuple(_SPECS.values())


def source_ids() -> tuple[str, ...]:
    return tuple(_SPECS)


def selectable_source_ids() -> tuple[str, ...]:
    """管理设置里可勾选的源清单（literature_settings.supported_sources() 的来源）。"""
    return tuple(spec.id for spec in _SPECS.values() if spec.selectable)


def get_source(source_id: str, *, client: Any | None = None) -> Any | None:
    """取一个源的免凭据适配器；未注册或该源没有默认构建方式时返回 None。

    ``client`` 用于沿用调用模块自己的客户端注入缝（daily_feed / paper_import /
    actions_wiki 的 get_*_client 模块属性是既有测试与调试面，不因收拢注册表
    而作废）；缺省时各源包自己的模块级单例。
    """
    spec = _SPECS.get(source_id.strip().lower())
    if spec is None or spec.default_factory is None:
        return None
    return spec.default_factory(client)


def require_source(source_id: str, *, client: Any | None = None) -> Any:
    """同 get_source，但未注册时抛错——内置源缺席属于装配错误，不该静默降级。"""
    adapter = get_source(source_id, client=client)
    if adapter is None:
        raise LookupError(f"literature source not registered: {source_id!r}")
    return adapter


def resolvers_for(kind: str) -> tuple[str, ...]:
    """能解析某类标识的源 id，按 resolve_priority 升序（= 级联尝试顺序）。

    arXiv 号：arxiv → openalex（限流兜底）；DOI：openalex；Corpus ID：semantic。
    顺序是注册元数据——paper_import 的解析级联从这里取，不再自行排布。
    """
    matched = [spec for spec in _SPECS.values() if kind in spec.resolve_kinds]
    matched.sort(key=lambda spec: spec.resolve_priority)
    return tuple(spec.id for spec in matched)


# ---- 内置源注册 ----

#: 共享的免凭据多源客户端（懒建：多数进程用不到这些源的免凭据形态）
_default_multi_source: MultiSourceClient | None = None


def _shared_multi_source() -> MultiSourceClient:
    global _default_multi_source
    if _default_multi_source is None:
        _default_multi_source = MultiSourceClient()
    return _default_multi_source


def _default_openalex(client: Any | None) -> OpenAlexAdapter:
    from app.services import literature

    return OpenAlexAdapter(client if client is not None else literature.get_openalex_client())


def _default_semantic(client: Any | None) -> SemanticScholarAdapter:
    from app.services import literature

    return SemanticScholarAdapter(client if client is not None else literature.get_s2_client())


def _default_arxiv(client: Any | None) -> ArxivAdapter:
    from app.services import literature

    return ArxivAdapter(client if client is not None else literature.get_arxiv_client())


def _build_openalex(ctx: SourceBuildContext) -> RotatingAdapter:
    from app.services.literature.openalex import OpenAlexClient

    keys = ctx.credential_pool("openalex") or [""]
    return RotatingAdapter(
        "openalex", [OpenAlexAdapter(OpenAlexClient(api_key=key or None)) for key in keys]
    )


def _build_semantic(ctx: SourceBuildContext) -> RotatingAdapter:
    from app.services.literature.semantic_scholar import SemanticScholarClient

    keys = ctx.credential_pool("semantic", ctx.app_settings.s2_api_key) or [""]
    return RotatingAdapter(
        "semantic",
        [SemanticScholarAdapter(SemanticScholarClient(api_key=key or None)) for key in keys],
    )


def _build_arxiv(ctx: SourceBuildContext) -> ArxivAdapter:  # noqa: ARG001 — 无凭据可用
    from app.services.literature.arxiv import ArxivClient

    return ArxivAdapter(ArxivClient())


def _multi_source_spec(name: str, *, selectable: bool = True) -> SourceSpec:
    return SourceSpec(
        id=name,
        build=lambda ctx: MultiSourceAdapter(name, ctx.multi_source),
        default_factory=lambda client: MultiSourceAdapter(name, client or _shared_multi_source()),
        selectable=selectable,
    )


def build_context(runtime_settings: Mapping[str, Any]) -> SourceBuildContext:
    """从管理员运行时设置构建一次注册表装配的上下文（discovery 用）。"""
    return SourceBuildContext(
        runtime_settings=runtime_settings,
        app_settings=get_settings(),
        multi_source=MultiSourceClient(
            provider_keys=runtime_settings.get("provider_keys")
            if isinstance(runtime_settings.get("provider_keys"), Mapping)
            else None
        ),
    )


# 注册顺序即 SUPPORTED_SOURCES 顺序（openalex → semantic → arxiv → 多源提供方），
# 与收拢前 literature_settings 的手写元组逐项一致；unpaywall 只注册不进可选清单。
register_source(
    SourceSpec(
        id="openalex",
        build=_build_openalex,
        default_factory=_default_openalex,
        resolve_kinds=("arxiv", "doi"),
        resolve_priority=1,
    )
)
register_source(
    SourceSpec(
        id="semantic",
        build=_build_semantic,
        default_factory=_default_semantic,
        resolve_kinds=("corpus_id",),
        resolve_priority=2,
    )
)
register_source(
    SourceSpec(
        id="arxiv",
        build=_build_arxiv,
        default_factory=_default_arxiv,
        resolve_kinds=("arxiv",),
        resolve_priority=0,  # arXiv 号先问 arXiv 本尊，限流才轮到 OpenAlex
    )
)
for _name in ("pubmed", "crossref", "europepmc", "hal", "core", "base", "sciverse"):
    register_source(_multi_source_spec(_name))
register_source(_multi_source_spec("unpaywall", selectable=False))
