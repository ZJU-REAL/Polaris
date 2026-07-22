"""发表机构 LLM 解析测试（services/affiliations.py + 接线）。

覆盖：服务函数（fake 数组写入 / 坏 JSON 兜底 None / 标题页截断 3000 字）、
wiki.fetch_extract 优先级（有全文走 LLM 且 OpenAlex 不被调 / LLM 失败回落
OpenAlex / 无全文直接 OpenAlex）、手动 fetch-pdf 路径补机构。
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx
from sqlalchemy import select

from app.agents.voyage import actions_wiki
from app.agents.voyage.actions import ActionContext
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.paper import Paper
from app.models.voyage import VoyageRun
from app.services.affiliations import _HEAD_CHARS, _MAX_TOKENS, extract_affiliations_llm
from app.services.literature import (
    ArxivClient,
    OpenAlexClient,
    SemanticScholarClient,
    reset_clients,
    set_clients,
)
from tests.conftest import add_paper, membership_of, register_and_login

FULL_TEXT = (
    "Great Paper Title\n"
    "Alice Zhang (Zhejiang University)  Bob Li (Google DeepMind)\n"
    "alice@zju.edu.cn\n\nAbstract: something interesting.\n"
)

OPENALEX_WORK = {
    "id": "https://openalex.org/W1",
    "title": "Affil Paper",
    "publication_year": 2026,
    "publication_date": "2026-01-02",
    "authorships": [
        {
            "author": {"display_name": "Carol"},
            "institutions": [{"display_name": "OpenAlex University"}],
        }
    ],
}


class _StubLLM:
    """记录调用参数的假路由器；error 给定时抛出（测调用失败兜底）。"""

    def __init__(self, content: str = "[]", error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    async def complete(self, stage, messages, **kwargs):
        self.calls.append({"stage": stage, "messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content)


def _paper(tmp_path, full_text: str | None) -> Paper:
    txt_path = None
    if full_text is not None:
        f = tmp_path / "full.txt"
        f.write_text(full_text, encoding="utf-8")
        txt_path = str(f)
    return Paper(title="Affil Paper", full_text_path=txt_path)


# ---- 服务函数单测（无 DB） ----


async def test_extract_affiliations_parses_array(tmp_path):
    llm = _StubLLM('好的，结果是：["Zhejiang University", "Zhejiang University", " MIT ", 42]')
    paper = _paper(tmp_path, FULL_TEXT)
    result = await extract_affiliations_llm(paper, llm=llm)
    assert result == ["Zhejiang University", "MIT"]  # 去重、strip、丢非字符串
    call = llm.calls[0]
    assert call["stage"] == "librarian"
    assert call["max_tokens"] == _MAX_TOKENS
    assert call["project_id"] is None  # stub 未传记账归属


async def test_extract_affiliations_truncates_head(tmp_path):
    # 3000 字以后的内容（LATE_MARKER）不应进 prompt
    text = FULL_TEXT + "x" * _HEAD_CHARS + "LATE_MARKER"
    llm = _StubLLM('["Zhejiang University"]')
    paper = _paper(tmp_path, text)
    assert await extract_affiliations_llm(paper, llm=llm) == ["Zhejiang University"]
    user_msg = llm.calls[0]["messages"][1].content
    assert "LATE_MARKER" not in user_msg
    assert user_msg.endswith(text[:_HEAD_CHARS].strip())
    assert FULL_TEXT.strip().splitlines()[0] in user_msg  # 标题页开头在


async def test_extract_affiliations_bad_json_returns_none(tmp_path):
    for content in ("解析不了，抱歉", '{"affiliations": "not an array"}', "[]", '["  "]'):
        paper = _paper(tmp_path, FULL_TEXT)
        assert await extract_affiliations_llm(paper, llm=_StubLLM(content)) is None


async def test_extract_affiliations_llm_error_returns_none(tmp_path):
    paper = _paper(tmp_path, FULL_TEXT)
    llm = _StubLLM(error=RuntimeError("boom"))
    assert await extract_affiliations_llm(paper, llm=llm) is None


async def test_extract_affiliations_no_fulltext_returns_none(tmp_path):
    llm = _StubLLM('["Zhejiang University"]')
    assert await extract_affiliations_llm(_paper(tmp_path, None), llm=llm) is None
    missing = Paper(title="T", full_text_path=str(tmp_path / "nope.txt"))
    assert await extract_affiliations_llm(missing, llm=llm) is None
    assert llm.calls == []


# ---- wiki.fetch_extract 接线（优先级 LLM > OpenAlex） ----


@pytest_asyncio.fixture
async def lit_clients(app):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        s2=SemanticScholarClient(redis=redis, api_key="", rate=10_000, backoff_base=0.0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )
    yield
    reset_clients()
    await redis.aclose()


async def _setup_scored_paper(
    client, tmp_path, *, full_text: str | None, published: bool
) -> tuple[uuid.UUID, VoyageRun]:
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "affil-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])
    txt_path = None
    if full_text is not None:
        f = tmp_path / "full.txt"
        f.write_text(full_text, encoding="utf-8")
        txt_path = str(f)
    async with get_sessionmaker()() as session:
        paper = await add_paper(session,
            project_id=project_id,
            source="snowball",
            status="scored",
            title="Affil Paper",
            doi="10.1234/affil.1",
            relevance_score=0.9,
            full_text_path=txt_path,
            published_at=datetime(2026, 1, 1, tzinfo=UTC) if published else None,
        )
        run = VoyageRun(
            kind="wiki_ingest",
            goal="ingest",
            status="executing",
            cursor=0,
            project_id=project_id,
            checkpoint={"params": {}},
        )
        session.add_all([paper, run])
        await session.commit()
        await session.refresh(paper)
        await session.refresh(run)
        return paper.id, run


async def _load_paper(paper_id: uuid.UUID) -> Paper:
    async with get_sessionmaker()() as session:
        return (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one()


async def test_fetch_extract_prefers_llm_over_openalex(client, lit_clients, tmp_path):
    paper_id, run = await _setup_scored_paper(client, tmp_path, full_text=FULL_TEXT, published=True)
    with respx.mock(assert_all_called=False) as router:
        openalex_route = router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(200, json=OPENALEX_WORK)
        )
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        obs = await actions_wiki.fetch_extract(ctx, {})
    assert obs["succeeded"] == 1 and obs["failed"] == []
    assert not openalex_route.called  # 有全文 → 走 LLM，OpenAlex 不被调
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["Zhejiang University", "Google DeepMind"]  # fake LLM 数组
    async with get_sessionmaker()() as session:
        membership = await membership_of(
            session, project_id=run.project_id, paper_id=paper_id
        )
        assert membership.status == "fetched"


async def test_fetch_extract_llm_failure_falls_back_to_openalex(
    client, lit_clients, tmp_path, monkeypatch
):
    async def _fail(paper, **kwargs):
        return None  # 模拟 LLM 解析失败（extract 内部失败即返回 None）

    monkeypatch.setattr(actions_wiki, "extract_affiliations_llm", _fail)
    paper_id, run = await _setup_scored_paper(
        client, tmp_path, full_text=FULL_TEXT, published=False
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(200, json=OPENALEX_WORK)
        )
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        obs = await actions_wiki.fetch_extract(ctx, {})
    assert obs["succeeded"] == 1
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["OpenAlex University"]  # OpenAlex 兜底
    assert paper.published_at is not None  # 顺带补 DOI 论文发表日期


async def test_fetch_extract_no_fulltext_uses_openalex(client, lit_clients, tmp_path, monkeypatch):
    llm_calls = {"n": 0}

    async def _count(paper, **kwargs):
        llm_calls["n"] += 1
        return ["Should Not Appear"]

    monkeypatch.setattr(actions_wiki, "extract_affiliations_llm", _count)
    paper_id, run = await _setup_scored_paper(client, tmp_path, full_text=None, published=False)
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(200, json=OPENALEX_WORK)
        )
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        await actions_wiki.fetch_extract(ctx, {})
    assert llm_calls["n"] == 0  # 无全文不调 LLM
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["OpenAlex University"]
    assert paper.published_at is not None


# ---- 手动 fetch-pdf 路径（此前不补机构） ----


def _make_pdf_bytes() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Great Paper Title. Alice Zhang, Zhejiang University.")
    data = doc.tobytes()
    doc.close()
    return data


async def test_fetch_pdf_backfills_affiliations(client, lit_clients):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "affil-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])
    async with get_sessionmaker()() as session:
        paper = await add_paper(session,
            project_id=project_id,
            source="manual",
            status="included",
            title="Affil Paper",
            arxiv_id="2404.11111",
        )
        session.add(paper)
        await session.commit()
        await session.refresh(paper)
        paper_id = paper.id
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(
            return_value=httpx.Response(200, content=_make_pdf_bytes())
        )
        resp = await client.post(f"/api/papers/{paper_id}/fetch-pdf", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["affiliations"] == ["Zhejiang University", "Google DeepMind"]
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["Zhejiang University", "Google DeepMind"]
