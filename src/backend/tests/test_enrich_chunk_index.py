"""三路径统一「分块 + 论文级向量」，块向量受 chat_fulltext_index 开关控制。

覆盖 enrich_paper（手动添加 / Daily 收录）与 papers.fetch_pdf（抓取 PDF）：
- 抽到全文即分块（无块才切，不重切丢已补向量）；
- 论文级向量照常产出；
- 块向量仅在用户开启开关时补齐，开关关时块行留着但 embedding 为空。
"""

import tempfile
import uuid
from pathlib import Path

import httpx
import respx
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.paper import PaperChunk
from app.services import paper_enrich
from app.services.literature import reset_clients, set_clients
from app.services.literature.arxiv import ArxivClient
from app.services.literature.openalex import OpenAlexClient
from tests.conftest import add_paper, make_project_with_library, register_and_login


async def _noop_emit(stage: str, status: str, detail: str | None = None) -> None:
    return None


def _write_fulltext() -> str:
    txt_dir = Path(tempfile.mkdtemp(prefix="polaris-enrich-"))
    txt = txt_dir / "p.txt"
    txt.write_text("规划方法的实现细节与实验。" * 200, encoding="utf-8")
    return str(txt)


async def _user(client, email: str, *, index_on: bool):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    if index_on:
        await client.patch(
            "/api/users/me/settings", json={"chat_fulltext_index": True}, headers=headers
        )
    me = (await client.get("/api/users/me", headers=headers)).json()
    return uuid.UUID(me["id"]), headers


async def _n_chunks(session, paper_id):
    return await session.scalar(
        select(func.count()).select_from(PaperChunk).where(PaperChunk.paper_id == paper_id)
    )


async def _n_embedded(session, paper_id):
    return await session.scalar(
        select(func.count())
        .select_from(PaperChunk)
        .where(PaperChunk.paper_id == paper_id, PaperChunk.embedding.is_not(None))
    )


async def _run_enrich(paper_id, *, user_id):
    async with get_sessionmaker()() as session:
        paper = await session.get(paper_enrich.Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=user_id, project_id=None, emit=_noop_emit
        )


# ---- enrich_paper：分块 + 论文级向量 + 块向量受开关 ----


async def test_enrich_chunks_but_leaves_block_vectors_when_index_off(client, fake_redis):
    """开关关：抽到全文→建块 + 论文级向量，但块行 embedding 留空。"""
    user_id, headers = await _user(client, "off@example.com", index_on=False)
    project_id, _ = await make_project_with_library(client, headers, name="enrich-off")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Off",
            doi="10.1/off", full_text_path=_write_fulltext(),
        )
        await session.commit()
        paper_id = paper.id

    await _run_enrich(paper_id, user_id=user_id)

    async with get_sessionmaker()() as session:
        paper = await session.get(paper_enrich.Paper, paper_id)
        assert paper.embedding is not None  # 论文级向量照常
        assert await _n_chunks(session, paper_id) > 0  # 已分块
        assert await _n_embedded(session, paper_id) == 0  # 开关关：块向量不补


async def test_enrich_embeds_blocks_when_index_on(client, fake_redis):
    """开关开：块行被嵌入。"""
    user_id, headers = await _user(client, "on@example.com", index_on=True)
    project_id, _ = await make_project_with_library(client, headers, name="enrich-on")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="On",
            doi="10.1/on", full_text_path=_write_fulltext(),
        )
        await session.commit()
        paper_id = paper.id

    await _run_enrich(paper_id, user_id=user_id)

    async with get_sessionmaker()() as session:
        n = await _n_chunks(session, paper_id)
        assert n > 0
        assert await _n_embedded(session, paper_id) == n  # 全部块被嵌入


async def test_enrich_does_not_reslice_existing_chunks(client, fake_redis):
    """已有块的论文再 enrich 不重切（无块才切），保住已补的块向量。"""
    user_id, headers = await _user(client, "keep@example.com", index_on=True)
    project_id, _ = await make_project_with_library(client, headers, name="enrich-keep")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Keep",
            doi="10.1/keep", full_text_path=_write_fulltext(),
        )
        await session.commit()
        paper_id = paper.id
        # 预置一个「手工块」并打上可辨识向量
        session.add(PaperChunk(paper_id=paper_id, seq=0, text="手工块", embedding=[0.5] * 1024))
        await session.commit()

    await _run_enrich(paper_id, user_id=user_id)

    async with get_sessionmaker()() as session:
        chunks = (
            (await session.execute(select(PaperChunk).where(PaperChunk.paper_id == paper_id)))
            .scalars()
            .all()
        )
        assert len(chunks) == 1  # 没被重切
        assert chunks[0].text == "手工块"


# ---- fetch_pdf：分块 + 论文级向量 + 块向量受开关 ----


@respx.mock
async def test_fetch_pdf_builds_paper_vector_and_gated_blocks(client, fake_redis):
    """fetch_pdf 现在产出论文级向量 + 分块；块向量受开关（此处开关开→嵌）。"""
    redis = fake_redis
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        openalex=OpenAlexClient(redis=redis, mailto="t@example.org"),
    )
    try:
        respx.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(
            return_value=httpx.Response(200, content=b"%PDF-1.4 not-a-real-pdf")
        )
        user_id, headers = await _user(client, "fetch@example.com", index_on=True)
        project_id, _ = await make_project_with_library(client, headers, name="fetch")
        # 预置全文（extract 抽不出也不影响；chunk 走已有 full_text_path）
        async with get_sessionmaker()() as session:
            paper = await add_paper(
                session, project_id=uuid.UUID(project_id), title="Fetch",
                arxiv_id="2406.22222", full_text_path=_write_fulltext(),
            )
            await session.commit()
            paper_id = paper.id

        from app.services.papers import fetch_pdf

        async with get_sessionmaker()() as session:
            paper = await session.get(paper_enrich.Paper, paper_id)
            await fetch_pdf(session, paper, user_id=user_id, project_id=None)

        async with get_sessionmaker()() as session:
            paper = await session.get(paper_enrich.Paper, paper_id)
            assert paper.pdf_path  # 下载落盘
            assert paper.embedding is not None  # 新增：论文级向量
            n = await _n_chunks(session, paper_id)
            assert n > 0
            assert await _n_embedded(session, paper_id) == n  # 开关开→块被嵌
    finally:
        reset_clients()
