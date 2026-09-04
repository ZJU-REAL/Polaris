"""引文意图分类 + OpenAlex 对齐（#639）：解析/分类确定性、对齐匹配规则、增量钩子。"""

import uuid

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.llm.router import get_llm_router
from app.models.paper import Paper
from app.models.paper_citation import PaperCitation
from app.services import paper_enrich
from app.services.citation_graph import (
    classify_citation_intents,
    ensure_citation_edges,
    parse_references,
)
from app.services.literature import reset_clients, set_clients
from app.services.literature.openalex import OpenAlexClient
from app.services.openalex_align import align_paper, openalex_id_of
from tests.conftest import add_paper, make_project_with_library, register_and_login

# 五种上下文句各触发 fake provider 的一条确定性规则（core/llm/fake.py 对齐）：
# [1] 无关键词 → background；[2] we follow → method；[3] compared → comparison；
# [4] consistent with → support；[5] however/unlike → contrast
FULL_TEXT = """Deep Agent Survey

Introduction

Early systems established the paradigm of tool-augmented reasoning [1].
We follow the planning method of [2] to build our agent loop.
Our results are compared against the strong baseline of [3].
Our findings are consistent with the observations reported in [4].
However, unlike the claims made in [5], we find scaling alone is insufficient.

References

[1] Alice Zhang. Foundations of Tool-Augmented Reasoning Systems. Journal of AI, 2020.
[2] Bob Li. Planning With Language Models For Agent Tasks. NeurIPS, 2021.
[3] Carol Wei. A Strong Baseline For Agent Benchmarks. ICML, 2022.
[4] Dan Wu. Observations On Agent Generalization Behavior. 2023.
[5] Eve Lin. Scaling Is All You Need For Agents. 2024.
"""

EXPECTED_INTENTS = {
    1: "background",
    2: "method",
    3: "comparison",
    4: "support",
    5: "contrast",
}


async def _noop_emit(stage, status, detail=None):  # noqa: ARG001
    return None


def _write_fulltext(tmp_path, text=FULL_TEXT):
    path = tmp_path / f"{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest_asyncio.fixture
async def oa_redis():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield redis
    await redis.aclose()


def _work(openalex_id, title, *, year=2024, abstract_words=None, venue=None):
    """构造 OpenAlex work 响应（_simplify 的输入形态）。"""
    inverted = (
        {w: [i] for i, w in enumerate(abstract_words)} if abstract_words else None
    )
    return {
        "id": openalex_id,
        "title": title,
        "publication_year": year,
        "publication_date": f"{year}-01-01",
        "abstract_inverted_index": inverted,
        "doi": "https://doi.org/10.9999/aligned",
        "cited_by_count": 42,
        "primary_location": {
            "landing_page_url": "https://example.org/paper",
            "source": {"id": "S1", "display_name": venue or "Test Venue", "issn_l": None},
        },
        "authorships": [],
    }


# ---- 1. 参考文献解析（确定性，无 LLM） ----


def test_parse_references_numbered_with_contexts():
    refs = parse_references(FULL_TEXT)
    assert [r.index for r in refs] == [1, 2, 3, 4, 5]
    assert refs[0].raw.startswith("Alice Zhang.")
    # 上下文句按编号回挂
    assert "tool-augmented reasoning [1]" in refs[0].context
    assert "We follow the planning method" in refs[1].context
    assert refs[4].context.startswith("However, unlike")


def test_parse_references_unnumbered_falls_back_without_contexts():
    text = (
        "Body text citing prior work in prose form.\n\n"
        "References\n\n"
        "Alice Zhang. Foundations of Tool-Augmented Reasoning Systems. 2020.\n"
        "Bob Li. Planning With Language Models For Agent Tasks. 2021.\n"
    )
    refs = parse_references(text)
    assert [r.index for r in refs] == [1, 2]
    assert all(r.context is None for r in refs)  # 对不上号 → 全部走降级路径


def test_parse_references_absent_section_yields_nothing():
    assert parse_references("just a body, no reference section at all.") == []


# ---- 2. 建边 + 意图分类（fake provider 确定性） ----


async def test_edges_built_and_intents_classified_deterministically(client, tmp_path):
    token = await register_and_login(client, email="cite@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="cite-proj")

    async with get_sessionmaker()() as session:
        # 池内先放一篇标题与 [2] 条目相同的论文 → 应完成池内对齐
        cited = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Planning With Language Models For Agent Tasks",
        )
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Deep Agent Survey",
            abstract="A survey of agent systems.",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        created = await ensure_citation_edges(session, paper)
        assert created == 5
        # 幂等：已建过则跳过，不重复灌边
        assert await ensure_citation_edges(session, paper) == 0
        classified = await classify_citation_intents(session, paper, get_llm_router())
        assert classified == 5
        await session.commit()

        rows = (
            (
                await session.execute(
                    select(PaperCitation)
                    .where(PaperCitation.citing_paper_id == paper.id)
                    .order_by(PaperCitation.ref_index)
                )
            )
            .scalars()
            .all()
        )
        assert {r.ref_index: r.intent for r in rows} == EXPECTED_INTENTS
        assert all(r.confidence is not None for r in rows)
        by_index = {r.ref_index: r for r in rows}
        assert by_index[2].cited_paper_id == cited.id  # 池内对齐命中
        assert by_index[1].cited_paper_id is None


# ---- 3. 增量钩子：enrich_paper 顺带建边分类；对齐开关默认关 ----


async def test_enrich_hook_builds_citation_edges(client, tmp_path):
    token = await register_and_login(client, email="hook@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="hook-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Deep Agent Survey",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        paper_id = paper.id

    with respx.mock(assert_all_called=False) as router:
        oa_route = router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(404)
        )
        async with get_sessionmaker()() as session:
            paper = await session.get(Paper, paper_id)
            await paper_enrich.enrich_paper(
                session, paper, target=None, user_id=None, project_id=None, emit=_noop_emit
            )
    # 对齐开关在测试环境默认关（conftest 置 0）：不许有任何出网调用
    assert oa_route.call_count == 0

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(PaperCitation).where(PaperCitation.citing_paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 5
    assert {r.ref_index: r.intent for r in rows} == EXPECTED_INTENTS


async def test_enrich_hook_aligns_openalex_when_enabled(
    client, tmp_path, monkeypatch, oa_redis
):
    token = await register_and_login(client, email="hookalign@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="hookalign-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Aligned By Enrich Hook Paper",
            doi="10.5555/hook",
        )
        await session.commit()
        paper_id = paper.id

    monkeypatch.setattr(get_settings(), "openalex_align_on_enrich", True)
    set_clients(openalex=OpenAlexClient(redis=oa_redis, mailto="t@example.org"))
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r"https://api\.openalex\.org/works/doi:.*").mock(
                return_value=httpx.Response(
                    200,
                    json=_work(
                        "https://openalex.org/W777", "Aligned By Enrich Hook Paper"
                    ),
                )
            )
            async with get_sessionmaker()() as session:
                paper = await session.get(Paper, paper_id)
                await paper_enrich.enrich_paper(
                    session, paper, target=None, user_id=None, project_id=None, emit=_noop_emit
                )
    finally:
        reset_clients()

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        assert openalex_id_of(paper) == "W777"


# ---- 4. OpenAlex 对齐匹配规则（mock 响应） ----


async def test_align_by_doi_backfills_only_empty_fields(client, oa_redis):
    token = await register_and_login(client, email="align@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="align-proj")
    oa_client = OpenAlexClient(redis=oa_redis, mailto="t@example.org")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Aligned Paper About Agents",
            doi="10.5555/aligned",
            venue="Original Venue",  # 已有值不许被覆盖
        )
        await session.commit()

        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r"https://api\.openalex\.org/works/doi:.*").mock(
                return_value=httpx.Response(
                    200,
                    json=_work(
                        "https://openalex.org/W123",
                        "Aligned Paper About Agents",
                        abstract_words=["Backfilled", "abstract", "text."],
                        venue="OpenAlex Venue",
                    ),
                )
            )
            assert await align_paper(session, paper, client=oa_client) is True
        await session.commit()
        await session.refresh(paper)
        assert openalex_id_of(paper) == "W123"
        assert paper.abstract == "Backfilled abstract text."  # 空字段回填
        assert paper.venue == "Original Venue"  # 已有值保持不动
        # 幂等：已有 id 不再出网
        assert await align_paper(session, paper, client=oa_client) is False


async def test_align_rejects_doi_hit_with_mismatched_title(client, oa_redis):
    token = await register_and_login(client, email="align2@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="align2-proj")
    oa_client = OpenAlexClient(redis=oa_redis, mailto="t@example.org")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="A Completely Different Subject Entirely",
            doi="10.5555/wrong",
        )
        await session.commit()
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r"https://api\.openalex\.org/works/doi:.*").mock(
                return_value=httpx.Response(
                    200, json=_work("https://openalex.org/W999", "An Unrelated Paper Title")
                )
            )
            assert await align_paper(session, paper, client=oa_client) is False
        assert openalex_id_of(paper) is None


async def test_align_by_title_year_fuzzy_match(client, oa_redis):
    token = await register_and_login(client, email="align3@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="align3-proj")
    oa_client = OpenAlexClient(redis=oa_redis, mailto="t@example.org")

    search_payload = {
        "results": [
            _work("https://openalex.org/W1", "Some Other Work On Agents", year=2023),
            # 标题只差标点大小写、年份差 1（预印本 vs 正式发表）→ 应命中
            _work("https://openalex.org/W2", "Fuzzy matched agent paper:  a study", year=2023),
        ]
    }
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Fuzzy Matched Agent Paper: A Study",
            year=2022,
        )
        await session.commit()
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r"https://api\.openalex\.org/works\?.*").mock(
                return_value=httpx.Response(200, json=search_payload)
            )
            assert await align_paper(session, paper, client=oa_client) is True
        await session.commit()
        await session.refresh(paper)
        assert openalex_id_of(paper) == "W2"


async def test_align_by_title_rejects_wrong_year(client, oa_redis):
    token = await register_and_login(client, email="align4@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="align4-proj")
    oa_client = OpenAlexClient(redis=oa_redis, mailto="t@example.org")

    search_payload = {
        "results": [_work("https://openalex.org/W3", "Year Gated Agent Paper Title", year=2018)]
    }
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Year Gated Agent Paper Title",
            year=2024,  # 与候选差 6 年 → 拒绝
        )
        await session.commit()
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r"https://api\.openalex\.org/works\?.*").mock(
                return_value=httpx.Response(200, json=search_payload)
            )
            assert await align_paper(session, paper, client=oa_client) is False
        assert openalex_id_of(paper) is None


# ---- 5. 详情端点：按意图分组 ----


async def test_citations_endpoint_groups_by_intent(client, tmp_path):
    token = await register_and_login(client, email="group@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="group-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Deep Agent Survey",
            abstract="A survey.",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        await ensure_citation_edges(session, paper)
        await classify_citation_intents(session, paper, get_llm_router())
        await session.commit()
        paper_id = paper.id

    resp = await client.get(f"/api/papers/{paper_id}/citations", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    # 分组顺序 = CITATION_INTENTS 声明序；每组恰好一条（fixture 各触发一档）
    assert [g["intent"] for g in body["groups"]] == [
        "background",
        "method",
        "comparison",
        "support",
        "contrast",
    ]
    method_item = body["groups"][1]["items"][0]
    assert method_item["ref_index"] == 2
    assert "Planning With Language Models" in method_item["cited_ref_raw"]
    assert "We follow the planning method" in method_item["context"]

    # 未登录不可读
    anon = await client.get(f"/api/papers/{paper_id}/citations")
    assert anon.status_code == 401


async def test_citations_endpoint_unclassified_group_last(client, tmp_path):
    token = await register_and_login(client, email="group2@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="group2-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Deep Agent Survey",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        await ensure_citation_edges(session, paper)  # 只建边不分类 → 全部未分类
        await session.commit()
        paper_id = paper.id

    resp = await client.get(f"/api/papers/{paper_id}/citations", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert [g["intent"] for g in body["groups"]] == [None]
    assert len(body["groups"][0]["items"]) == 5
