"""统一只读工具注册表 app/tools：注册/派发/渲染 + 若干库内工具的端到端调用。"""

import uuid

import pytest

import app.tools as tools
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.idea import Idea
from app.tools import ToolContext
from tests.conftest import add_concept, add_paper

from .conftest import register_and_login


async def _project(client, email="tools@example.com") -> uuid.UUID:
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "tools-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _ctx(project_id: uuid.UUID) -> ToolContext:
    return ToolContext(project_id=project_id, llm=LLMRouter())


# ---- 纯单元：注册 / 派发 / 渲染 ----


def test_registry_has_expected_tools():
    names = tools.known_tools()
    for expected in [
        "search_papers",
        "read_wiki",
        "read_fulltext",
        "get_concept",
        "list_concepts",
        "search_chunks",
        "get_paper",
        "knowledge_graph",
        "global_search",
        "list_ideas",
        "get_idea",
        "list_experiments",
        "get_experiment",
        "get_fact_pack",
        "external_search",
        "get_references",
        "get_citations",
        "lookup_paper",
    ]:
        assert expected in names, expected
    # 每个工具的 input_schema 都是合法 JSON-schema object
    for spec in tools.list_tools():
        assert spec.input_schema.get("type") == "object", spec.name
        assert spec.read_only is True


def test_render_tool_specs_subset():
    text = tools.render_tool_specs(["search_papers", "get_concept"])
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("- search_papers {") and "库内检索论文" in lines[0]
    assert '"query"' in lines[0] and '"mode"?' in lines[0]  # required vs optional 标注


async def test_run_tool_unknown_and_bad_args():
    ctx = _ctx(uuid.uuid4())
    with pytest.raises(ValueError, match="未知工具"):
        await tools.run_tool(ctx, "nope", {})
    with pytest.raises(ValueError, match="JSON 对象"):
        await tools.run_tool(ctx, "search_papers", ["not", "a", "dict"])  # type: ignore[arg-type]


# ---- 端到端：库内只读工具（走真实 DB，离线 fake LLM） ----


async def test_library_tools_end_to_end(client):
    project_id = await _project(client)
    async with get_sessionmaker()() as session:
        concept = await add_concept(session,
            project_id=project_id,
            name="Retrieval",
            slug="retrieval",
            definition="按需取信息",
            category="method",
        )
        paper = await add_paper(session,
            project_id=project_id,
            source="manual",
            title="Retrieval Augmented Agents",
            abstract="retrieval augmented generation for research agents",
            tldr="RAG for agents",
            wiki_content="## TL;DR\n\nRAG 综述 [[Retrieval]]",
            status="compiled",
            concepts=[concept],  # 构造时上链，避免异步惰性加载
        )
        idea = Idea(project_id=project_id, title="RAG idea", summary="用检索增强想法构建")
        session.add_all([paper, idea])
        await session.commit()
        paper_id = str(paper.id)

    ctx = _ctx(project_id)

    # search_papers：sqlite 无 pgvector → 关键词降级
    res = await tools.run_tool(ctx, "search_papers", {"query": "retrieval", "mode": "semantic"})
    assert res["mode"] == "keyword"
    assert any(p["paper_id"] == paper_id for p in res["results"])

    # get_paper：元数据 + 概念标签
    res = await tools.run_tool(ctx, "get_paper", {"paper_id": paper_id})
    assert res["title"] == "Retrieval Augmented Agents"
    assert "Retrieval" in res["concepts"]

    # read_wiki
    res = await tools.run_tool(ctx, "read_wiki", {"paper_id": paper_id})
    assert "RAG 综述" in res["wiki"]

    # get_concept + list_concepts
    res = await tools.run_tool(ctx, "get_concept", {"name": "Retrieval"})
    assert res["found"] is True and res["category"] == "method"
    res = await tools.run_tool(ctx, "list_concepts", {})
    assert any(c["name"] == "Retrieval" for c in res["concepts"])

    # global_search 跨实体
    res = await tools.run_tool(ctx, "global_search", {"q": "RAG"})
    assert any(h["type"] == "idea" for h in res["hits"])

    # list_ideas / get_idea
    res = await tools.run_tool(ctx, "list_ideas", {})
    assert len(res["ideas"]) == 1
    idea_id = res["ideas"][0]["idea_id"]
    res = await tools.run_tool(ctx, "get_idea", {"idea_id": idea_id})
    assert res["title"] == "RAG idea"


async def test_cross_project_isolation(client):
    """A 项目的工具上下文取不到 B 项目的论文。"""
    proj_a = await _project(client, email="a@example.com")
    proj_b = await _project(client, email="b@example.com")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=proj_b,
            source="manual",
            title="B secret",
            status="compiled",
        )
        session.add(paper)
        await session.commit()
        b_paper_id = str(paper.id)

    ctx_a = _ctx(proj_a)
    with pytest.raises(ValueError, match="库内不存在"):
        await tools.run_tool(ctx_a, "get_paper", {"paper_id": b_paper_id})
