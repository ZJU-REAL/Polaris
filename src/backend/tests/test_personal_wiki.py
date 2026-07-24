"""个人版 wiki 按需编译 + 三层 wiki 解析 + 书架快照手动刷新（P5b）。

LLM 走 core/llm 的 fake provider（POLARIS_LLM_FAKE_FALLBACK=1，conftest）。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library import UserLibraryEntry
from app.models.llm_config import LLMUsage
from app.models.paper import Paper
from app.models.topic_shelf import TopicPaper
from tests.conftest import add_paper, membership_of, register_and_login


async def _setup(client, *, name="pw-proj", email="alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_pool_only_paper(**fields) -> str:
    """建一篇「在池但不在任何库」的论文（个人补充入库的典型状态）。"""
    async with get_sessionmaker()() as session:
        paper = Paper(**({"title": "Pool Only Paper", "source": "manual"} | fields))
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def _entry_of(paper_id: str) -> UserLibraryEntry | None:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(UserLibraryEntry).where(
                    UserLibraryEntry.last_paper_id == uuid.UUID(paper_id)
                )
            )
        ).scalar_one_or_none()


# ---- 个人版 wiki 编译 ----


async def test_compile_personal_wiki_writes_entry_and_bills_user(client):
    project_id, headers = await _setup(client)
    paper_id = await _seed_pool_only_paper(
        title="Personal Supplement Paper", abstract="Outside any library."
    )

    resp = await client.post(
        f"/api/papers/{paper_id}/personal-wiki", json={"topic_id": project_id}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paper_id"] == paper_id
    assert "TL;DR" in body["wiki_content"]  # fake librarian 的通用模板输出
    assert body["model"]

    # 结果写进本人个人库条目（无条目自动建）
    entry = await _entry_of(paper_id)
    assert entry is not None
    assert entry.wiki_content == body["wiki_content"]
    assert entry.saved is False  # 只写 wiki，不代收藏

    # 用量归因：user + topic（stage=librarian 至少一条）
    async with get_sessionmaker()() as session:
        usages = (
            (
                await session.execute(
                    select(LLMUsage).where(LLMUsage.stage == "librarian")
                )
            )
            .scalars()
            .all()
        )
        assert usages, "librarian 编译必须记账"
        assert all(u.user_id is not None for u in usages)
        assert any(str(u.project_id) == project_id for u in usages)


async def test_compile_personal_wiki_without_topic(client):
    """不带 topic_id 也能编译（纯通用模板，归因只挂用户）。"""
    _, headers = await _setup(client, name="pw-no-topic", email="carol@example.com")
    paper_id = await _seed_pool_only_paper(title="No Topic Paper")

    resp = await client.post(f"/api/papers/{paper_id}/personal-wiki", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    entry = await _entry_of(paper_id)
    assert entry is not None and entry.wiki_content


async def test_compile_personal_wiki_conflicts_and_missing(client):
    project_id, headers = await _setup(client, name="pw-conflict", email="dave@example.com")
    # 已有库版 wiki → 409（应直接展示库版）
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Compiled In Library",
            status="compiled",
            wiki_content="# 库版",
        )
        await session.commit()
        compiled_id = str(paper.id)
    resp = await client.post(
        f"/api/papers/{compiled_id}/personal-wiki", json={}, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "LIBRARY_WIKI_EXISTS"

    # 池中不存在 → 404
    resp = await client.post(
        f"/api/papers/{uuid.uuid4()}/personal-wiki", json={}, headers=headers
    )
    assert resp.status_code == 404

    # topic_id 不是自己的课题 → 404
    outsider_project, _ = await _setup(client, name="pw-other", email="erin@example.com")
    paper_id = await _seed_pool_only_paper(title="Foreign Topic Paper")
    resp = await client.post(
        f"/api/papers/{paper_id}/personal-wiki",
        json={"topic_id": outsider_project},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_compile_personal_wiki_in_progress_409(client, monkeypatch):
    """并发防抖：同一 paper × user 编译进行中时，二次请求 409 COMPILE_IN_PROGRESS。"""
    import asyncio

    from app.services import personal_wiki as pw
    from app.services.wiki_compile import CompiledWiki

    _, headers = await _setup(client, name="pw-race", email="race@example.com")
    paper_id = await _seed_pool_only_paper(title="Race Paper")

    started, release = asyncio.Event(), asyncio.Event()

    async def slow_compile(paper, **kwargs):
        started.set()
        await release.wait()
        return CompiledWiki(content="# 慢编译", model="fake")

    monkeypatch.setattr(pw, "compile_paper", slow_compile)
    first = asyncio.create_task(
        client.post(f"/api/papers/{paper_id}/personal-wiki", json={}, headers=headers)
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    resp = await client.post(f"/api/papers/{paper_id}/personal-wiki", json={}, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "COMPILE_IN_PROGRESS"
    release.set()
    resp = await asyncio.wait_for(first, timeout=5)
    assert resp.status_code == 200, resp.text
    entry = await _entry_of(paper_id)
    assert entry is not None and entry.wiki_content == "# 慢编译"


# ---- 三层 wiki 解析（库版实时 > 个人版 > 快照） ----


async def test_shelf_resolves_personal_wiki_between_live_and_snapshot(client):
    project_id, headers = await _setup(client, name="tier-proj", email="frank@example.com")
    paper_id = await _seed_pool_only_paper(title="Tiered Paper", arxiv_id="2403.00003")

    # 入架（此时无任何 wiki）→ none
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["wiki_source"] == "none"

    # 编译个人版 → personal
    resp = await client.post(f"/api/papers/{paper_id}/personal-wiki", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    personal_wiki = resp.json()["wiki_content"]
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    item = resp.json()["items"][0]
    assert item["wiki_source"] == "personal"
    assert item["wiki_content"] == personal_wiki

    # 手动给书架行塞一份快照：个人版仍然优先于快照
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(TopicPaper).where(TopicPaper.paper_id == uuid.UUID(paper_id))
            )
        ).scalar_one()
        row.wiki_snapshot = "# 旧快照"
        await session.commit()
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    assert resp.json()["items"][0]["wiki_source"] == "personal"

    # 库后来收录并编译 → 库版实时优先
    async with get_sessionmaker()() as session:
        from app.models.library_direction import LibraryPaper
        from tests.conftest import ensure_project_library

        library = await ensure_project_library(session, uuid.UUID(project_id))
        session.add(
            LibraryPaper(
                library_id=library.id,
                paper_id=uuid.UUID(paper_id),
                status="compiled",
                wiki_content="# 库版解读",
            )
        )
        await session.commit()
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    item = resp.json()["items"][0]
    assert item["wiki_source"] == "live"
    assert item["wiki_content"] == "# 库版解读"

    # 个人版属于本人：其他成员（无个人条目）在库版消失后回退快照
    bob = await register_and_login(client, email="tier-bob@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "tier-bob@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 204
    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        await session.delete(membership)
        await session.commit()
    resp = await client.get(
        f"/api/projects/{project_id}/shelf", headers={"Authorization": f"Bearer {bob}"}
    )
    item = resp.json()["items"][0]
    assert item["wiki_source"] == "snapshot"
    assert item["wiki_content"] == "# 旧快照"


# ---- 书架快照手动刷新 ----


async def test_refresh_snapshot_from_live_and_personal(client):
    project_id, headers = await _setup(client, name="snap-proj", email="grace@example.com")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Refreshable Paper",
            status="compiled",
            wiki_content="# 第一版",
        )
        await session.commit()
        paper_id = str(paper.id)
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201
    first_snapshot_at = resp.json()["snapshot_at"]

    # 库版重编译后手动刷新 → 快照跟上新库版
    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        membership.wiki_content = "# 第二版"
        await session.commit()
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/{paper_id}/refresh-snapshot", headers=headers
    )
    assert resp.status_code == 200, resp.text
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(TopicPaper).where(TopicPaper.paper_id == uuid.UUID(paper_id))
            )
        ).scalar_one()
        assert row.wiki_snapshot == "# 第二版"
        assert row.snapshot_at is not None
    assert resp.json()["snapshot_at"] >= first_snapshot_at

    # 库版消失、只剩个人版 → 刷新拷个人版
    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        membership.wiki_content = None
        await session.commit()
        entry = (
            await session.execute(
                select(UserLibraryEntry).where(
                    UserLibraryEntry.last_paper_id == uuid.UUID(paper_id)
                )
            )
        ).scalar_one()
        entry.wiki_content = "# 个人版"
        await session.commit()
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/{paper_id}/refresh-snapshot", headers=headers
    )
    assert resp.status_code == 200
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(TopicPaper).where(TopicPaper.paper_id == uuid.UUID(paper_id))
            )
        ).scalar_one()
        assert row.wiki_snapshot == "# 个人版"


async def test_refresh_snapshot_no_source_409(client):
    project_id, headers = await _setup(client, name="snap-none", email="henry@example.com")
    paper_id = await _seed_pool_only_paper(title="Sourceless Paper")
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201
    # 入架同步建了个人库条目但无 wiki → 仍然无源
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/{paper_id}/refresh-snapshot", headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "NO_WIKI_SOURCE"

    # 书架上没有的论文 → 404
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/{uuid.uuid4()}/refresh-snapshot", headers=headers
    )
    assert resp.status_code == 404
