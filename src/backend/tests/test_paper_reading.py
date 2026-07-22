"""PDF 阅读 + AI 伴读（docs/api-lit.md §1、§3）：respx mock arxiv、fake LLM，全离线。"""

import json
import uuid

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.llm_config import LLMUsage
from app.models.paper import Paper
from app.services.literature import reset_clients, set_clients
from app.services.literature.arxiv import ArxivClient
from app.services.literature.pdf_extract import save_pdf
from tests.conftest import add_paper, register_and_login


def _pdf_bytes(text: str = "hello polaris") -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


@pytest_asyncio.fixture
async def lit_clients():
    """注入 min_interval=0 + fakeredis 缓存的 arxiv 客户端（respx 拦 HTTP）。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(arxiv=ArxivClient(redis=redis, min_interval=0))
    yield
    reset_clients()
    await redis.aclose()


async def _setup(client, *, arxiv_id: str | None = "2406.00001", email: str = "alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "read-proj"}, headers=headers)
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        paper = await add_paper(session,
            project_id=uuid.UUID(project_id),
            source="arxiv" if arxiv_id else "manual",
            arxiv_id=arxiv_id,
            title="Readable Paper",
            abstract="A paper about reading agents.",
            status="included",
        )
        session.add(paper)
        await session.commit()
        paper_id = str(paper.id)
    return project_id, headers, paper_id


async def test_get_pdf_404_then_serves_file(client):
    project_id, headers, paper_id = await _setup(client)

    resp = await client.get(f"/api/papers/{paper_id}/pdf", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PDF_NOT_AVAILABLE"

    content = _pdf_bytes()
    path = save_pdf(paper_id, content)
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        paper.pdf_path = str(path)
        await session.commit()

    resp = await client.get(f"/api/papers/{paper_id}/pdf", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == content

    # 非项目成员也可读（P5c：方向库全实验室可读，PDF 原文免费共享）
    other = await register_and_login(client, email="outsider@example.com")
    resp = await client.get(
        f"/api/papers/{paper_id}/pdf", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 200
    assert resp.content == content


@respx.mock
async def test_fetch_pdf_success_idempotent_and_400(client, lit_clients):
    project_id, headers, paper_id = await _setup(client)
    route = respx.get(url__regex=r"https://arxiv\.org/pdf/2406\.00001").mock(
        return_value=httpx.Response(200, content=_pdf_bytes("full text here"))
    )

    resp = await client.post(f"/api/papers/{paper_id}/fetch-pdf", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["pdf_available"] is True
    assert route.call_count == 1
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        assert paper.pdf_path and paper.full_text_path  # 下载成功后补抽全文

    # 幂等：已有 PDF 不再下载
    resp = await client.post(f"/api/papers/{paper_id}/fetch-pdf", headers=headers)
    assert resp.status_code == 200
    assert route.call_count == 1

    # 无 arxiv_id 的论文 → 400
    _, headers2, paper2 = await _setup(client, arxiv_id=None, email="noarxiv@example.com")
    resp = await client.post(f"/api/papers/{paper2}/fetch-pdf", headers=headers2)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "PDF_SOURCE_UNSUPPORTED"


@respx.mock
async def test_fetch_pdf_download_failure_502(client, lit_clients):
    project_id, headers, paper_id = await _setup(client)
    respx.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(return_value=httpx.Response(500))
    resp = await client.post(f"/api/papers/{paper_id}/fetch-pdf", headers=headers)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "PDF_FETCH_FAILED"


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


async def test_chat_sse_stream_and_usage(client):
    project_id, headers, paper_id = await _setup(client)

    async with client.stream(
        "POST",
        f"/api/papers/{paper_id}/chat",
        json={
            "question": "这篇论文的方法是什么？",
            "history": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "在"},
            ],
        },
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = (await resp.aread()).decode("utf-8")

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[-1] == "done" and "delta" in kinds and "error" not in kinds
    answer = "".join(d["text"] for e, d in events if e == "delta")
    assert "fake 伴读" in answer  # fake provider 的确定性 reading 响应
    assert "这篇论文的方法是什么？" in answer
    usage = dict(events[-1][1])["usage"]
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0

    # 用量落库：stage=reading，归属项目
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(LLMUsage).where(LLMUsage.stage == "reading")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert str(rows[0].project_id) == project_id
        assert rows[0].completion_tokens > 0

    # 非项目成员也可伴读（P5c：库成员论文全员可读；费用记个人、无课题上下文）
    other = await register_and_login(client, email="chat-outsider@example.com")
    resp = await client.post(
        f"/api/papers/{paper_id}/chat",
        json={"question": "hi"},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 200


async def test_chat_with_referenced_papers(client):
    """/ 选中的其他文献作为参考上下文：emit sources 帧；跨项目引用被过滤；不选则无 sources。"""
    project_id, headers, paper_id = await _setup(client)
    # 另建一个项目放一篇「外部」论文——跨项目引用必须被过滤掉
    resp = await client.post("/api/projects", json={"name": "other-proj"}, headers=headers)
    foreign_pid = resp.json()["id"]

    async with get_sessionmaker()() as session:
        referenced = await add_paper(session,
            project_id=uuid.UUID(project_id),
            source="manual",
            title="Fara-1.5 Learning Environments",
            abstract="Fara-1.5 scalable environments for computer use agents.",
            tldr="Fara-1.5 一句话总结：可扩展的 CUA 学习环境。",
            status="included",
        )
        foreign = await add_paper(session,
            project_id=uuid.UUID(foreign_pid),
            source="manual",
            title="Foreign Paper",
            abstract="unrelated",
            status="included",
        )
        session.add_all([referenced, foreign])
        await session.commit()
        referenced_id, foreign_id = str(referenced.id), str(foreign.id)

    async with client.stream(
        "POST",
        f"/api/papers/{paper_id}/chat",
        json={
            "question": "和 Fara-1.5 比较一下",
            "history": [],
            "context_paper_ids": [referenced_id, foreign_id],
        },
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        body = (await resp.aread()).decode("utf-8")

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds
    items = events[0][1]["items"]
    ids = {it["paper_id"] for it in items}
    assert referenced_id in ids  # 同项目被引用的论文进入来源
    assert foreign_id not in ids  # 跨项目的被过滤
    assert items[0]["title"] == "Fara-1.5 Learning Environments"

    # 不传 context_paper_ids 时向后兼容：不发 sources 帧
    async with client.stream(
        "POST",
        f"/api/papers/{paper_id}/chat",
        json={"question": "这篇讲什么", "history": []},
        headers=headers,
    ) as resp:
        body2 = (await resp.aread()).decode("utf-8")
    assert "event: sources" not in body2
