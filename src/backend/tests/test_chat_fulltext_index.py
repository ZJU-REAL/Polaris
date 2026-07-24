"""可选全文索引（Part B）：个人设置开关 + 按 scope 批量建全文索引 + 入队门控。"""

import tempfile
import uuid
from pathlib import Path

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.core.llm.router import get_llm_router
from app.models.paper import PaperChunk
from app.services.fulltext_index import index_papers_fulltext
from tests.conftest import add_paper, register_and_login

# ---- 1. PATCH /users/me/settings ----


async def test_patch_settings_and_read_back(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    # 缺省未设置
    me = (await client.get("/api/users/me", headers=headers)).json()
    assert me.get("settings") in (None, {})

    resp = await client.patch(
        "/api/users/me/settings", json={"chat_fulltext_index": True}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["chat_fulltext_index"] is True

    me = (await client.get("/api/users/me", headers=headers)).json()
    assert me["settings"]["chat_fulltext_index"] is True

    # 关回去也能读到
    resp = await client.patch(
        "/api/users/me/settings", json={"chat_fulltext_index": False}, headers=headers
    )
    assert resp.json()["settings"]["chat_fulltext_index"] is False


# ---- 3/4. 索引服务 ----


async def _project(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "idx", "definition": {"statement": "LLM agent 规划"}},
        headers=headers,
    )
    return uuid.UUID(resp.json()["id"]), headers


async def test_index_papers_fulltext_builds_chunks_and_skips_no_source(client):
    project_id, _ = await _project(client)
    txt_dir = Path(tempfile.mkdtemp(prefix="polaris-idx-"))

    async with get_sessionmaker()() as session:
        txt = txt_dir / "p.txt"
        txt.write_text("规划方法细节。" * 300, encoding="utf-8")
        with_text = await add_paper(
            session,
            project_id=project_id,
            title="Has Full Text",
            abstract="a",
            year=2025,
            status="compiled",
            full_text_path=str(txt),
        )
        # 无 arxiv、无本地全文 → best-effort 跳过，不抛
        no_source = await add_paper(
            session,
            project_id=project_id,
            title="No Source",
            abstract="b",
            year=2025,
            status="candidate",
        )
        await session.commit()
        paper_ids = [with_text.id, no_source.id]

    async with get_sessionmaker()() as session:
        result = await index_papers_fulltext(
            session, paper_ids=paper_ids, llm=get_llm_router(), user_id=None
        )
    assert result["papers"] == 2
    assert result["indexed"] == 1
    assert result["skipped"] == 1
    assert result["embed_error"] is None

    async with get_sessionmaker()() as session:
        n_with = await session.scalar(
            select(func.count()).select_from(PaperChunk).where(PaperChunk.paper_id == paper_ids[0])
        )
        n_without = await session.scalar(
            select(func.count()).select_from(PaperChunk).where(PaperChunk.paper_id == paper_ids[1])
        )
    assert n_with > 0
    assert n_without == 0
    # fake provider 支持 embed → 全部补齐
    assert result["embedded"] == n_with

    # 幂等：已建分段的论文再跑不重建、不重复计入 indexed
    async with get_sessionmaker()() as session:
        again = await index_papers_fulltext(
            session, paper_ids=paper_ids, llm=get_llm_router(), user_id=None
        )
    assert again["indexed"] == 0
    assert again["skipped"] == 2


# ---- 6. 入队端点 + 409 门控 ----


async def _enable_index(client, headers):
    resp = await client.patch(
        "/api/users/me/settings", json={"chat_fulltext_index": True}, headers=headers
    )
    assert resp.status_code == 200


async def test_shelf_index_rebuild_gated(client, queue_stub):
    project_id, headers = await _project(client)

    # 设置关 → 409
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/index/rebuild", headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "INDEXING_DISABLED"
    assert queue_stub.jobs == []

    # 设置开 → 200 + 入队
    await _enable_index(client, headers)
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/index/rebuild", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert "queued" in resp.json()
    assert len(queue_stub.jobs) == 1
    func_name, args, _ = queue_stub.jobs[0]
    assert func_name == "index_papers_fulltext_task"
    assert args[0] == "shelf"
    assert args[2] == str(project_id)


async def test_shelf_index_rebuild_reports_indexable_counts(client, queue_stub):
    """返回体含 indexable / no_fulltext：书架上有全文的计入 indexable，其余计跳过。"""
    project_id, headers = await _project(client)
    await _enable_index(client, headers)

    txt_dir = Path(tempfile.mkdtemp(prefix="polaris-idx-"))
    txt = txt_dir / "p.txt"
    txt.write_text("规划方法细节。" * 50, encoding="utf-8")
    async with get_sessionmaker()() as session:
        with_text = await add_paper(
            session, project_id=project_id, title="Has Full Text",
            status="compiled", full_text_path=str(txt),
        )
        no_text = await add_paper(
            session, project_id=project_id, title="No Full Text", status="candidate",
        )
        await session.commit()
        # 入书架（两篇都上架，才会进 shelf_paper_ids）
        from app.services.topic_shelf import add_to_shelf

        user = (await client.get("/api/users/me", headers=headers)).json()
        for p in (with_text, no_text):
            await add_to_shelf(
                session, project_id=project_id, paper_id=p.id, user_id=uuid.UUID(user["id"])
            )
        await session.commit()

    resp = await client.post(
        f"/api/projects/{project_id}/shelf/index/rebuild", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued"] == 2
    assert body["indexable"] == 1
    assert body["no_fulltext"] == 1


async def test_shelf_index_rebuild_requires_member(client, queue_stub):
    _, headers = await _project(client)
    await _enable_index(client, headers)
    resp = await client.post(
        f"/api/projects/{uuid.uuid4()}/shelf/index/rebuild", headers=headers
    )
    assert resp.status_code == 404
    assert queue_stub.jobs == []


async def test_personal_index_rebuild_gated(client, queue_stub):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/api/library/index/rebuild", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "INDEXING_DISABLED"
    assert queue_stub.jobs == []

    await _enable_index(client, headers)
    resp = await client.post("/api/library/index/rebuild", headers=headers)
    assert resp.status_code == 200, resp.text
    assert "queued" in resp.json()
    assert len(queue_stub.jobs) == 1
    func_name, args, _ = queue_stub.jobs[0]
    assert func_name == "index_papers_fulltext_task"
    assert args[0] == "personal"
    assert args[2] is None
