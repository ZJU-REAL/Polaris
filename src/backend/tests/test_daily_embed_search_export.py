"""每日新论文：论文级向量口径统一 + 无条件建向量/分段 + 补建 + 语义检索回退 + 引用导出。"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.fake import fake_embedding
from app.models.daily_feed import DailyFeedEntry
from app.models.paper import Paper
from app.models.vectors import PaperVector
from app.services.paper_enrich import paper_embedding_text
from tests.conftest import register_and_login
from tests.test_daily_feed import _rss_entry, _run_sync
from tests.vector_helpers import get_paper_vector, set_paper_vector

pytestmark = pytest.mark.asyncio


async def _admin_headers(client) -> dict[str, str]:
    token = await register_and_login(client)  # 首个注册用户 = admin
    return {"Authorization": f"Bearer {token}"}


def _paper(**kw) -> Paper:
    base = {
        "source": "arxiv",
        "dedup_key": uuid.uuid4().hex,
        "title": "Retrieval Augmented Agents",
        "authors": [{"name": "Ada Lovelace"}, {"name": "Alan Turing"}],
        "abstract": "We study retrieval augmented agents.",
        "tldr": "TLDR sentence.",
    }
    return Paper(**(base | kw))


# ---- 1. 统一的论文级向量文本口径 ----


async def test_paper_embedding_text_is_title_authors_abstract():
    text = paper_embedding_text(_paper())
    assert "Retrieval Augmented Agents" in text
    assert "Ada Lovelace" in text and "Alan Turing" in text
    assert "We study retrieval augmented agents." in text
    assert "TLDR" not in text  # 新口径不含 tldr

    # 缺字段不炸；超长截断到 2000
    assert paper_embedding_text(_paper(authors=None, abstract=None)).strip()
    long_one = _paper(abstract="x" * 5000)
    assert len(paper_embedding_text(long_one)) == 2000


async def test_ingest_uses_shared_embedding_text():
    """ingest 上链批量嵌入与手动补全共用同一个文本函数（每日池的口径在同步测试里对账）。"""
    from app.agents.voyage import actions_wiki
    from app.services import paper_enrich

    assert actions_wiki.paper_embedding_text is paper_enrich.paper_embedding_text


async def test_embed_paper_uses_shared_text(client):
    await register_and_login(client)
    from app.services.paper_enrich import embed_paper

    paper = _paper()
    async with get_sessionmaker()() as session:
        session.add(paper)
        await session.flush()
        await embed_paper(session, paper)
        await session.commit()
        assert await get_paper_vector(session, paper.id) == fake_embedding(
            paper_embedding_text(paper)
        )


# ---- 2. 每日池建向量：开关 + 幂等 + 补建 ----


async def _papers_in_pool() -> list[Paper]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Paper).join(DailyFeedEntry, DailyFeedEntry.paper_id == Paper.id)
            )
        ).scalars()
        return list(rows)


async def test_sync_embeds_every_paper_and_only_missing(client, monkeypatch):
    """每日推送的论文一律建论文级向量（不再受管理员开关管），且幂等只补缺失的。"""
    feed = {"cs.AI": [_rss_entry("2607.10001", "Alpha"), _rss_entry("2607.10002", "Beta")]}

    # 无需任何开关：同步即建向量，文本口径 = 标题+作者+摘要
    result = await _run_sync(monkeypatch, feed)
    assert result["created"] == 2 and result["embedded"] == 2
    papers = await _papers_in_pool()
    async with get_sessionmaker()() as session:
        for paper in papers:
            row = await session.execute(
                select(PaperVector).where(PaperVector.paper_id == paper.id)
            )
            vector_row = row.scalar_one()
            assert list(vector_row.embedding) == fake_embedding(paper_embedding_text(paper))
            assert vector_row.built_at is not None  # 构建时间记下了
            assert vector_row.model  # 模型名记下了
            assert vector_row.space  # 属于哪个向量空间也记下了

    # 已有向量的不重嵌（哨兵向量原样保留）
    sentinel = [0.5] * 1024
    async with get_sessionmaker()() as session:
        await set_paper_vector(session, papers[0].id, sentinel)
    result = await _run_sync(monkeypatch, feed)
    assert result["embedded"] == 0
    async with get_sessionmaker()() as session:
        assert await get_paper_vector(session, papers[0].id) == pytest.approx(sentinel)


async def test_daily_papers_get_abstract_chunk(client, monkeypatch):
    """每日推送不下 PDF，论文靠「标题 + 摘要」兜底块进入文献对话的检索范围。"""
    from app.models.paper import PaperChunk

    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.10009", "Delta")]})
    papers = await _papers_in_pool()
    assert papers and all(p.full_text_path is None for p in papers)
    async with get_sessionmaker()() as session:
        chunks = (
            (
                await session.execute(
                    select(PaperChunk).where(PaperChunk.paper_id == papers[0].id)
                )
            )
            .scalars()
            .all()
        )
    assert len(chunks) == 1  # 无全文 → 单个兜底块
    assert chunks[0].source == "abstract"
    assert papers[0].title in chunks[0].text


async def test_backfill_counts(client, monkeypatch):
    headers = await _admin_headers(client)
    # 同步已把向量建好，故补建端点这里全是「已有」
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.10003", "Gamma")]})

    resp = await client.post("/api/admin/settings/daily-embed/backfill", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"embedded": 0, "skipped": 1, "failed": 0}

    # 非 admin 无权
    other = await register_and_login(client, email="bob@example.com")
    resp = await client.post(
        "/api/admin/settings/daily-embed/backfill", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 403


# ---- 3. 语义检索（sqlite 下回退关键词） ----


async def test_semantic_mode_falls_back_on_sqlite(client, monkeypatch):
    headers = await _admin_headers(client)
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.10004", "Delta Networks")]})

    resp = await client.get(
        "/api/daily/papers", params={"mode": "semantic", "q": "Delta"}, headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode_used"] == "keyword"  # sqlite 无 pgvector → 回退
    assert body["total"] == 1 and body["items"][0]["title"] == "Delta Networks"

    # 默认仍是关键词模式，且响应带 mode_used
    resp = await client.get("/api/daily/papers", headers=headers)
    assert resp.json()["mode_used"] == "keyword"

    # 非法 mode → 422
    resp = await client.get("/api/daily/papers", params={"mode": "fuzzy"}, headers=headers)
    assert resp.status_code == 422


# ---- 4. 引用导出 ----


async def test_daily_export_citations(client, monkeypatch):
    headers = await _admin_headers(client)
    await _run_sync(
        monkeypatch,
        {"cs.AI": [_rss_entry("2607.10005", "Epsilon Study"), _rss_entry("2607.10006", "Zeta")]},
    )
    resp = await client.get("/api/daily/papers", headers=headers)
    items = resp.json()["items"]
    by_title = {i["title"]: i["paper_id"] for i in items}

    # 全量 bibtex
    resp = await client.get("/api/daily/export/citations", headers=headers)
    assert resp.status_code == 200
    assert (
        'attachment; filename="polaris-daily-citations.bib"' in resp.headers["content-disposition"]
    )
    body = resp.text
    assert body.count("@") == 2
    assert "Epsilon Study" in body and "Zeta" in body
    assert "eprint = {2607.10005}" in body

    # ids 子集 + csl-json
    resp = await client.get(
        "/api/daily/export/citations",
        params={"format": "csl-json", "ids": by_title["Zeta"]},
        headers=headers,
    )
    assert resp.status_code == 200
    assert "polaris-daily-citations.json" in resp.headers["content-disposition"]
    data = json.loads(resp.text)
    assert len(data) == 1 and data[0]["title"] == "Zeta"
    assert data[0]["author"][0]["family"] == "Lovelace"

    # 窗口外的 id（随便一个不在池里的）→ 落选
    resp = await client.get(
        "/api/daily/export/citations",
        params={"format": "csl-json", "ids": str(uuid.uuid4())},
        headers=headers,
    )
    assert json.loads(resp.text) == []

    # 非法 format / 非法 ids → 422
    resp = await client.get(
        "/api/daily/export/citations", params={"format": "endnote"}, headers=headers
    )
    assert resp.status_code == 422
    resp = await client.get(
        "/api/daily/export/citations", params={"ids": "not-a-uuid"}, headers=headers
    )
    assert resp.status_code == 422

    # 未登录不给导
    resp = await client.get("/api/daily/export/citations")
    assert resp.status_code == 401
