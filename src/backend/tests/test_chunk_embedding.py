"""全文分段的向量化：批次并发发出，写入顺序做，结果与逐批一致。"""

import asyncio

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.paper import PaperChunk
from app.models.vectors import PaperChunkVector
from app.services import chunks as chunks_service
from app.services import embedding as embedding_service
from tests.conftest import add_paper, make_project_with_library, register_and_login


async def _library_with_chunks(client, count: int):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="embed-lib", statement="向量化测试库"
    )
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=project_id, title="Chunked Paper", status="compiled"
        )
        for seq in range(count):
            session.add(
                PaperChunk(paper_id=paper.id, seq=seq, text=f"chunk body {seq}", source="fulltext")
            )
        await session.commit()
    return library_id


async def test_embedding_batches_run_concurrently_after_the_first(client, monkeypatch):
    monkeypatch.setattr(chunks_service, "EMBED_BATCH", 2)
    library_id = await _library_with_chunks(client, 11)  # 6 批
    real = embedding_service.embed_documents
    state = {"now": 0, "peak": 0, "order": []}

    async def tracked(session, texts, **kwargs):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        state["order"].append(state["now"])
        try:
            await asyncio.sleep(0.02)
            return await real(session, texts, **kwargs)
        finally:
            state["now"] -= 1

    monkeypatch.setattr(embedding_service, "embed_documents", tracked)
    async with get_sessionmaker()() as session:
        embedded, error = await chunks_service.embed_pending_chunks(
            session, library_id=library_id, llm=LLMRouter()
        )
        assert (embedded, error) == (11, None)
        vectors = (
            await session.execute(select(func.count()).select_from(PaperChunkVector))
        ).scalar_one()
        assert vectors == 11
        # 再跑一遍没有待补的分段
        assert await chunks_service.embed_pending_chunks(
            session, library_id=library_id, llm=LLMRouter()
        ) == (0, None)
    # 第一批单独做（由它定下向量空间），其余并发、不超过上限
    assert state["order"][0] == 1 and state["order"][1] == 1
    assert 1 < state["peak"] <= chunks_service.EMBED_CONCURRENCY


async def test_a_failed_batch_stops_with_what_was_already_embedded(client, monkeypatch):
    monkeypatch.setattr(chunks_service, "EMBED_BATCH", 2)
    library_id = await _library_with_chunks(client, 6)  # 3 批
    real = embedding_service.embed_documents
    calls = {"n": 0}

    async def second_fails(session, texts, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("gateway said no")
        return await real(session, texts, **kwargs)

    monkeypatch.setattr(embedding_service, "embed_documents", second_fails)
    async with get_sessionmaker()() as session:
        embedded, error = await chunks_service.embed_pending_chunks(
            session, library_id=library_id, llm=LLMRouter()
        )
    assert embedded == 2 and error == "RuntimeError: gateway said no"
