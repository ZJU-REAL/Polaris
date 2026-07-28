"""检索不得返回回收站里的论文。

删掉的论文又被搜出来，比搜不到更让人困惑——尤其它在列表里已经不见了。
关键词路径能在 sqlite 上真跑；向量路径是 pgvector 裸 SQL，测试库是 sqlite，
只能做源码级守卫（挡住有人把过滤删掉），这一点在下面的用例里写明。
"""

import inspect
import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import PaperChunk
from app.services import chunks as chunks_service
from app.services import papers as papers_service
from app.services import user_library as user_library_service
from tests.conftest import add_paper, register_and_login


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "trash-search", "statement": "agent planning"},
        headers=headers,
    )
    project_id = uuid.UUID(resp.json()["id"])
    async with get_sessionmaker()() as session:
        kept = await add_paper(
            session,
            project_id=project_id,
            title="Kept Planning Paper",
            abstract="planning agents in the wild",
            status="compiled",
            relevance_score=0.9,
        )
        trashed = await add_paper(
            session,
            project_id=project_id,
            title="Trashed Planning Paper",
            abstract="planning agents in the wild",
            status="excluded",
            relevance_score=0.2,
        )
        session.add_all([kept, trashed])
        await session.commit()
        return project_id, headers, kept.id, trashed.id


async def test_keyword_paper_search_skips_the_recycle_bin(client):
    project_id, _headers, kept_id, trashed_id = await _setup(client)
    async with get_sessionmaker()() as session:
        rows = await papers_service.keyword_search_papers(
            session, project_id=project_id, q="Planning", limit=20
        )
    found = {view.id for view, _score in rows}
    assert kept_id in found
    assert trashed_id not in found


async def test_keyword_chunk_search_skips_the_recycle_bin(client):
    """文献对话的关键词降级路径同样不能引用回收站里的论文。"""
    project_id, _headers, kept_id, trashed_id = await _setup(client)
    async with get_sessionmaker()() as session:
        for pid in (kept_id, trashed_id):
            session.add(
                PaperChunk(paper_id=pid, seq=0, text="planning agents in the wild")
            )
        await session.commit()

        from app.services.libraries import get_source_library_ids

        library_ids = await get_source_library_ids(session, project_id)
        hits = await chunks_service.keyword_search_chunks(
            session, library_ids=library_ids, paper_ids=None, q="planning", limit=20
        )
    paper_ids = {c.paper_id for c, _score in hits}
    assert kept_id in paper_ids
    assert trashed_id not in paper_ids


def test_vector_search_paths_filter_the_recycle_bin():
    """向量检索走 pgvector 裸 SQL，sqlite 测试库跑不到，这里做源码级守卫。

    删掉任一处过滤，这条会失败——比没有守卫强，但它确实不是端到端验证。
    """
    paper_sql = inspect.getsource(papers_service.semantic_search_papers)
    assert "lp.status = ANY" in paper_sql, "库论文向量检索漏了回收站过滤"

    chunk_sql = inspect.getsource(chunks_service.semantic_search_chunks)
    assert "lp.status = ANY" in chunk_sql, "分块向量检索漏了回收站过滤"

    personal = inspect.getsource(user_library_service.semantic_saved_entries)
    assert "trashed_at.is_(None)" in personal, "个人库向量检索漏了回收站过滤"


async def test_recycle_bin_listing_still_shows_them(client):
    """过滤只作用于检索：回收站页面本身当然还要能看到它们。"""
    project_id, headers, _kept_id, trashed_id = await _setup(client)
    resp = await client.get(f"/api/projects/{project_id}/papers?status=excluded", headers=headers)
    assert resp.status_code == 200, resp.text
    assert str(trashed_id) in {row["id"] for row in resp.json()["items"]}


async def test_chunk_search_by_paper_ids_is_unaffected(client):
    """显式给定 paper_ids 的分支（书架/个人库对话）不受库成员状态影响。"""
    project_id, _headers, kept_id, trashed_id = await _setup(client)
    async with get_sessionmaker()() as session:
        for pid in (kept_id, trashed_id):
            session.add(
                PaperChunk(paper_id=pid, seq=0, text="planning agents in the wild")
            )
        await session.commit()
        hits = await chunks_service.keyword_search_chunks(
            session,
            library_ids=None,
            paper_ids=[kept_id, trashed_id],
            q="planning",
            limit=20,
        )
    assert {c.paper_id for c, _ in hits} == {kept_id, trashed_id}
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(PaperChunk))).scalars().first() is not None
