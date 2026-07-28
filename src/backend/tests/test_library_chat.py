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
        json={"name": "lib-chat", "statement": "LLM agent 规划"},
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


# ---- 多轮引用记忆（#194）----


async def _project_and_router(project_id):
    from sqlalchemy import select as _select

    from app.core.llm.router import get_llm_router
    from app.models.project import Project

    session = get_sessionmaker()()
    project = (
        await session.execute(_select(Project).where(Project.id == project_id))
    ).scalar_one()
    return session, project, get_llm_router()


async def test_history_carries_its_own_citation_legend(client):
    """历史里的 [n] 必须自带对照表，否则模型分不清它和本轮的 [n] 是不是同一篇。"""
    from app.services import library_chat as lc

    project_id, headers, ids = await _setup(client)
    await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)

    session, project, llm = await _project_and_router(project_id)
    async with session:
        messages, _sources = await lc.build_library_messages(
            session,
            project=project,
            question="这篇文章具体做了什么？",
            history=[
                lc.HistoryTurn(role="user", content="planning 有哪些方法？"),
                lc.HistoryTurn(
                    role="assistant",
                    content="主流是树搜索 [1]。",
                    cited_paper_ids=[uuid.UUID(ids[0])],
                ),
            ],
            llm=llm,
        )

    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert len(assistant_msgs) == 1
    # 对照表里给出的是标题，模型据此认得出「这篇文章」是谁
    assert "引用对照" in assistant_msgs[0].content
    assert "Planning with Tree Search" in assistant_msgs[0].content
    # 系统提示要讲清楚编号不跨轮沿用
    assert "不要沿用历史轮次的编号" in messages[0].content


async def test_previously_cited_paper_is_pinned_into_context(client, monkeypatch):
    """追问「这篇文章怎么样」检索不回原文时，上一轮引用过的论文要补进上下文。

    否则模型认得出标题，却没有正文可依据，只能答「未检索到相关内容」。
    """
    from app.services import library_chat as lc

    project_id, headers, ids = await _setup(client)
    await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)
    cited = uuid.UUID(ids[1])  # 上一轮引用的是第二篇

    async def only_first_paper(session, **kwargs):
        """检索只命中第一篇，模拟追问句检索不回目标论文。"""
        rows = (
            await session.execute(
                select(PaperChunk).where(PaperChunk.paper_id == uuid.UUID(ids[0])).limit(2)
            )
        ).scalars().all()
        return [(c, 0.9) for c in rows]

    monkeypatch.setattr(lc, "_retrieve_chunks", only_first_paper)

    session, project, llm = await _project_and_router(project_id)
    async with session:
        messages, sources = await lc.build_library_messages(
            session,
            project=project,
            question="这篇文章具体做了什么？",
            history=[
                lc.HistoryTurn(
                    role="assistant",
                    content="可以看 [1]。",
                    cited_paper_ids=[cited],
                )
            ],
            llm=llm,
        )

    source_ids = [s.paper_id for s in sources]
    assert str(cited) in source_ids, "上一轮引用的论文没有被钉进本轮上下文"
    # 检索命中的那篇仍在，且排在钉进来的前面（更贴题的占小编号）
    assert source_ids[0] == ids[0]
    assert "Reflexion Self-Improvement" in messages[0].content
