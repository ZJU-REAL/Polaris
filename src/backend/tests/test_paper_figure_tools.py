"""围绕 Paper 的图片/支撑工具：直接调用 + MCP image content block。"""

import base64
import io
import uuid

from PIL import Image

import app.tools as tools
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.paper import Concept, Paper
from app.services.literature.pdf_extract import figure_path
from app.tools import ToolContext, ToolResult

from .conftest import register_and_login


def _png(w: int = 80, h: int = 60) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 80)).save(buf, format="PNG")
    return buf.getvalue()


async def _project(client, email="fig@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "fig-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_paper_with_figures(project_id: str) -> str:
    figs = [
        {
            "index": 0,
            "page": 1,
            "width": 80,
            "height": 60,
            "caption": "方法总览图",
            "kind": "method",
            "important": True,
        },
        {
            "index": 1,
            "page": 3,
            "width": 80,
            "height": 60,
            "caption": "主实验结果",
            "kind": "experiment",
            "important": True,
        },
        {
            "index": 2,
            "page": 5,
            "width": 80,
            "height": 60,
            "caption": None,
            "kind": None,
            "important": False,
        },
    ]
    async with get_sessionmaker()() as session:
        paper = Paper(
            project_id=uuid.UUID(project_id),
            source="manual",
            title="Retrieval Augmented Generation",
            abstract="a retrieval augmented method with figures",
            status="compiled",
            figures=figs,
        )
        session.add(paper)
        await session.commit()
        paper_id = str(paper.id)
    # 真图落盘（只给重要图 0/1；index2 无文件）
    figure_path(paper_id, 0).write_bytes(_png())
    figure_path(paper_id, 1).write_bytes(_png())
    return paper_id


def _ctx(project_id: str) -> ToolContext:
    return ToolContext(project_id=uuid.UUID(project_id), llm=LLMRouter())


async def test_figure_tools_direct(client):
    project_id, _ = await _project(client)
    paper_id = await _seed_paper_with_figures(project_id)
    ctx = _ctx(project_id)

    # list：3 张元数据
    res = await tools.run_tool(ctx, "list_paper_figures", {"paper_id": paper_id})
    assert len(res["figures"]) == 3
    assert res["figures"][0]["kind"] == "method"

    # get_paper_figure：ToolResult + 1 张图
    res = await tools.run_tool(ctx, "get_paper_figure", {"paper_id": paper_id, "index": 0})
    assert isinstance(res, ToolResult)
    assert res.payload["caption"] == "方法总览图"
    assert len(res.images) == 1
    with Image.open(io.BytesIO(res.images[0].data)) as im:  # 解码得回真图
        assert im.format == "PNG"

    # get_paper_figures：默认只重要图 → 2 张
    res = await tools.run_tool(ctx, "get_paper_figures", {"paper_id": paper_id})
    assert len(res.images) == 2
    # 按 kind 过滤 → 只方法图 1 张
    res = await tools.run_tool(ctx, "get_paper_figures", {"paper_id": paper_id, "kind": "method"})
    assert len(res.images) == 1

    # find_figures：命中论文后收重要图
    res = await tools.run_tool(ctx, "find_figures", {"query": "retrieval", "kind": "experiment"})
    assert any(f["caption"] == "主实验结果" for f in res["figures"])

    # 引用条目
    res = await tools.run_tool(ctx, "get_paper_citation", {"paper_id": paper_id})
    assert res["format"] == "bibtex" and "@" in res["bibtex"]


async def test_get_figure_missing_index(client):
    project_id, _ = await _project(client, email="fig2@example.com")
    paper_id = await _seed_paper_with_figures(project_id)
    ctx = _ctx(project_id)
    try:
        await tools.run_tool(ctx, "get_paper_figure", {"paper_id": paper_id, "index": 9})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "无此图" in str(e)


async def test_related_papers_shared_concept(client):
    project_id, _ = await _project(client, email="fig3@example.com")
    async with get_sessionmaker()() as session:
        pid = uuid.UUID(project_id)
        concept = Concept(project_id=pid, name="RAG", slug="rag", definition="检索增强")
        p1 = Paper(
            project_id=pid, source="manual", title="Paper A", status="compiled", concepts=[concept]
        )
        p2 = Paper(
            project_id=pid, source="manual", title="Paper B", status="compiled", concepts=[concept]
        )
        session.add_all([p1, p2])
        await session.commit()
        p1_id = str(p1.id)
    res = await tools.run_tool(_ctx(project_id), "related_papers", {"paper_id": p1_id})
    assert any(r["title"] == "Paper B" and r["shared_concepts"] == 1 for r in res["related"])


async def test_mcp_get_figure_returns_image_block(client):
    """MCP tools/call get_paper_figure → content 里带 image content block（base64 PNG）。"""
    project_id, headers = await _project(client, email="fig4@example.com")
    paper_id = await _seed_paper_with_figures(project_id)

    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_paper_figure",
                "arguments": {"project_id": project_id, "paper_id": paper_id, "index": 0},
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    content = resp.json()["result"]["content"]
    images = [c for c in content if c["type"] == "image"]
    assert len(images) == 1
    assert images[0]["mimeType"] == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(images[0]["data"]))) as im:
        assert im.format == "PNG"
