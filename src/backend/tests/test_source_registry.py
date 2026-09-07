"""文献源注册表（#720）：注册缝、能力探测、解析级联顺序、单例注入缝。

注册表是全后端唯一的源分派点：SUPPORTED_SOURCES 派生、discovery 适配器装配、
旁路调用点取适配器都走它。这里测的是「缝本身」；各链路的等价性由既有链路
测试兜底（旁路改道后必须原样通过）。
"""

from typing import Any

import pytest

from app.services import literature_settings
from app.services.literature import reset_clients, set_clients, sources
from app.services.literature.arxiv import ArxivClient

pytestmark = pytest.mark.asyncio


class _FakeAdapter:
    name = "fakesource"

    async def search(self, request):  # pragma: no cover - 形状占位
        raise AssertionError("not exercised")


def _fake_spec(source_id: str = "fakesource", **overrides: Any) -> sources.SourceSpec:
    params: dict[str, Any] = {
        "id": source_id,
        "build": lambda ctx: _FakeAdapter(),
        "default_factory": lambda client: _FakeAdapter(),
    }
    params.update(overrides)
    return sources.SourceSpec(**params)


async def test_register_source_seam_and_derived_supported_list():
    baseline = sources.selectable_source_ids()
    # 内置源清单与收拢前的手写元组逐项一致（顺序也是）
    assert baseline == (
        "openalex",
        "semantic",
        "arxiv",
        "pubmed",
        "crossref",
        "europepmc",
        "hal",
        "core",
        "base",
        "sciverse",
    )
    assert "unpaywall" in sources.source_ids()  # 注册但不可选（OA 解析专用）

    sources.register_source(_fake_spec())
    try:
        assert "fakesource" in sources.source_ids()
        # 设置校验实时读注册表：新源立即可选，凭据面也认得
        assert "fakesource" in literature_settings.supported_sources()
        assert "fakesource" in literature_settings.credential_sources()
        # 重名注册直接拒绝，不静默覆盖
        with pytest.raises(ValueError, match="already registered"):
            sources.register_source(_fake_spec())
    finally:
        sources.unregister_source("fakesource")
    assert "fakesource" not in sources.source_ids()
    assert sources.selectable_source_ids() == baseline


async def test_build_adapter_registry_follows_registrations():
    from app.services.literature import runtime

    registry = await runtime.build_adapter_registry({})
    assert registry.names() == set(sources.source_ids())

    sources.register_source(_fake_spec())
    try:
        # 指纹并入注册清单：同一份设置下，注册新源后不会复用旧缓存
        registry = await runtime.build_adapter_registry({})
        assert "fakesource" in registry.names()
        assert isinstance(registry.get("fakesource"), _FakeAdapter)
    finally:
        sources.unregister_source("fakesource")
    registry = await runtime.build_adapter_registry({})
    assert "fakesource" not in registry.names()


async def test_capability_probing_and_resolver_order():
    arxiv = sources.require_source("arxiv")
    semantic = sources.require_source("semantic")
    openalex = sources.require_source("openalex")

    # 能力按源自愿实现，调用方 hasattr 探测（Protocol 文档约定）
    assert hasattr(arxiv, "fetch_new") and hasattr(arxiv, "download_pdf")
    assert not hasattr(semantic, "fetch_new") and not hasattr(openalex, "download_pdf")
    assert hasattr(semantic, "snowball")

    # 解析级联顺序是注册元数据：arXiv 号先问 arXiv 本尊，限流才轮到 OpenAlex
    assert sources.resolvers_for("arxiv") == ("arxiv", "openalex")
    assert sources.resolvers_for("doi") == ("openalex",)
    assert sources.resolvers_for("corpus_id") == ("semantic",)
    assert sources.resolvers_for("unknown-kind") == ()


async def test_default_adapter_wraps_singleton_and_honors_injection():
    class _StubArxiv:
        page_size = 7

        async def download_pdf(self, arxiv_id: str) -> bytes:
            return b"%PDF stub " + arxiv_id.encode()

    stub = _StubArxiv()
    set_clients(arxiv=stub)  # type: ignore[arg-type] — 结构性替身，与既有测试同款
    try:
        adapter = sources.require_source("arxiv")
        # 免凭据适配器包的是模块级单例：set_clients 注入立刻生效（限速/缓存/测试缝不变）
        assert adapter.client is stub
        assert adapter.page_size == 7
        assert await adapter.download_pdf("2608.00001") == b"%PDF stub 2608.00001"
    finally:
        reset_clients()

    # 调用方显式给客户端时（各模块的 get_*_client 注入缝），以显式为准
    own = ArxivClient()
    assert sources.require_source("arxiv", client=own).client is own

    with pytest.raises(LookupError):
        sources.require_source("never-registered")
    assert sources.get_source("never-registered") is None


async def test_resolve_capability_shapes():
    class _StubArxiv:
        async def fetch_by_ids(self, ids):
            assert ids == ["2608.00042"]
            return [{"arxiv_id": "2608.00042"}, {"arxiv_id": "2608.00042", "title": "Hit"}]

    entry = await sources.require_source("arxiv", client=_StubArxiv()).resolve(
        "arxiv", "2608.00042"
    )
    assert entry == {"arxiv_id": "2608.00042", "title": "Hit"}  # 取第一条有 title 的

    class _StubOpenAlex:
        async def get_by_doi(self, doi):
            return {"title": f"doi:{doi}"}

        async def get_by_arxiv(self, arxiv_id):
            return {"title": f"arxiv:{arxiv_id}"}

    openalex = sources.require_source("openalex", client=_StubOpenAlex())
    assert (await openalex.resolve("doi", "10.1/x"))["title"] == "doi:10.1/x"
    assert (await openalex.resolve("arxiv", "2608.1"))["title"] == "arxiv:2608.1"
    with pytest.raises(ValueError, match="cannot resolve"):
        await openalex.resolve("corpus_id", "1")

    class _StubS2:
        async def get_references(self, ref):
            return [{"title": "ref"}]

        async def get_citations(self, ref):
            return [{"title": "cit"}]

    merged = await sources.require_source("semantic", client=_StubS2()).snowball("arXiv:1")
    # 参考文献在前、施引在后——与旁路时代 refs + cits 的拼接顺序一致
    assert [row["title"] for row in merged] == ["ref", "cit"]
