"""文献知识底座（docs/api-lit.md §8）：全文分段索引 + 文献库对话 SSE。"""

import json
import uuid
from pathlib import Path

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.paper import PaperChunk
from app.services.chunks import CHUNK_MAX_CHARS, split_text
from tests.conftest import add_concept, add_paper, register_and_login


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None:
            events.append((event, data))
    return events


# ---- 切分（纯函数） ----


def test_split_text_paragraph_packing():
    text = "\n\n".join(f"第 {i} 段。" + "内容" * 300 for i in range(6))  # 每段 ~600 字符
    chunks = split_text(text)
    assert len(chunks) >= 3
    assert all(len(c) <= CHUNK_MAX_CHARS * 2 for c in chunks)
    # 确定性：同输入同输出
    assert chunks == split_text(text)
    # 超长单段硬切
    long_chunks = split_text("超长" * 3000)
    assert len(long_chunks) >= 3
    assert split_text("") == []


# ---- 索引重建 + 对话（API） ----


async def _setup(client, tmp_path_factory=None):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "lib-chat", "definition": {"statement": "LLM agent 规划"}},
        headers=headers,
    )
    project_id = uuid.UUID(resp.json()["id"])

    import tempfile

    txt_dir = Path(tempfile.mkdtemp(prefix="polaris-chunks-"))
    async with get_sessionmaker()() as session:
        papers = []
        seeds = [
            ("Planning with Tree Search", "planning agent tree search " + "规划方法细节。" * 200),
            ("Reflexion Self-Improvement", "reflexion self improve " + "自我反思细节。" * 200),
        ]
        for i, (title, body) in enumerate(seeds):
            txt = txt_dir / f"p{i}.txt"
            txt.write_text(body, encoding="utf-8")
            p = await add_paper(session,
                project_id=project_id,
                title=title,
                abstract=f"{title} abstract",
                tldr=f"{title} 的一句话总结",
                year=2025,
                relevance_score=0.9 - i * 0.1,
                status="compiled",
                full_text_path=str(txt),
            )
            session.add(p)
            papers.append(p)
        await session.commit()
        ids = [str(p.id) for p in papers]
    return project_id, headers, ids


async def test_rebuild_index_and_chunk_rows(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["papers_indexed"] == 2
    assert body["chunks_created"] > 0
    assert body["total_chunks"] == body["chunks_created"]
    # fake provider 支持 embed → 全部补齐向量
    assert body["embedded"] == body["chunks_created"] and body["embed_error"] is None

    # 幂等：再跑一次不重复建
    resp = await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)
    assert resp.json()["papers_indexed"] == 0

    async with get_sessionmaker()() as session:
        count = (
            await session.execute(select(func.count()).select_from(PaperChunk))
        ).scalar_one()
        assert count == body["total_chunks"]


async def test_library_chat_sse_with_sources(client):
    project_id, headers, ids = await _setup(client)
    await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)

    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/chat",
        json={"question": "planning 方向的主流方法有哪些？", "history": []},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = (await resp.aread()).decode("utf-8")

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds
    sources = events[0][1]["items"]
    assert len(sources) >= 1
    assert {"index", "paper_id", "title", "year"} <= set(sources[0])
    answer = "".join(d["text"] for e, d in events if e == "delta")
    assert "fake 文献综合" in answer and "[1]" in answer


async def test_library_chat_falls_back_without_chunks(client):
    """未建分段索引时退化用论文 TL;DR/摘要，仍能给出来源。"""
    project_id, headers, ids = await _setup(client)

    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/chat",
        json={"question": "有哪些论文？", "history": []},
        headers=headers,
    ) as resp:
        body = (await resp.aread()).decode("utf-8")
    events = _parse_sse(body)
    assert events[0][0] == "sources"
    assert len(events[0][1]["items"]) == 2  # 两篇高分论文兜底

    # 非成员 404
    other = await register_and_login(client, email="lib-outsider@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/chat",
        json={"question": "hi"},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


async def test_library_chat_survives_chunk_search_failure(client, monkeypatch):
    """检索层任何异常（如 paper_chunks 表未迁移）都不 500：降级论文摘要兜底。"""
    project_id, headers, ids = await _setup(client)

    from app.services import chunks as chunks_service

    async def boom(*args, **kwargs):
        raise RuntimeError("relation paper_chunks does not exist (simulated)")

    monkeypatch.setattr(chunks_service, "keyword_search_chunks", boom)
    monkeypatch.setattr(chunks_service, "semantic_search_chunks", boom)

    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/chat",
        json={"question": "有哪些论文？", "history": []},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        body = (await resp.aread()).decode("utf-8")
    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds
    assert len(events[0][1]["items"]) == 2  # 摘要兜底仍给出来源


async def test_library_chat_sources_include_concepts(client):
    """来源带概念清单，上下文带「概念：」行（回答里 [[双链]] 可点可跳的基础）。"""
    project_id, headers, ids = await _setup(client)

    from sqlalchemy import insert

    from app.models.paper import paper_concepts
    from app.services.concepts import wiki_slug

    async with get_sessionmaker()() as session:
        c = await add_concept(session,
            project_id=project_id, name="思维树", slug=wiki_slug("思维树"), category="method"
        )
        session.add(c)
        await session.flush()
        await session.execute(
            insert(paper_concepts).values(paper_id=uuid.UUID(ids[0]), concept_id=c.id)
        )
        await session.commit()

    await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)
    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/chat",
        json={"question": "planning tree search 有什么方法？", "history": []},
        headers=headers,
    ) as resp:
        body = (await resp.aread()).decode("utf-8")
    events = _parse_sse(body)
    items = events[0][1]["items"]
    by_paper = {it["paper_id"]: it for it in items}
    assert "思维树" in by_paper[ids[0]]["concepts"]
    assert {"status", "relevance"} <= set(items[0])
