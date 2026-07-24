"""P6 重复论文合并（任务 3）：全表 repoint（含冲突分支）、候选发现、合并权限。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library import UserLibraryEntry
from app.models.library_direction import LibraryPaper
from app.models.paper import (
    Paper,
    PaperChunk,
    PaperHighlight,
    PaperNote,
    PaperUserMeta,
    paper_concepts,
)
from app.models.publication import UserPublication
from app.models.topic_shelf import TopicPaper
from app.models.user import User
from app.services import paper_merge as merge_service
from app.services.libraries import get_library_for_project
from tests.conftest import add_concept, add_paper, ensure_project_library, register_and_login


async def _setup(client, *, email="merge-owner@example.com", name="合并方向"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return headers, resp.json()["id"]


async def _user_id(session, email):
    return (await session.execute(select(User.id).where(User.email == email))).scalar_one()


async def test_merge_papers_full_repoint_with_conflicts(client):
    headers, project_id = await _setup(client)
    _headers_b, project_b = await _setup(client, email="merge-b@example.com", name="第二方向")

    async with get_sessionmaker()() as session:
        owner_id = await _user_id(session, "merge-owner@example.com")
        other_id = await _user_id(session, "merge-b@example.com")
        pid = uuid.UUID(project_id)
        pid_b = uuid.UUID(project_b)
        lib_a = await ensure_project_library(session, pid)
        lib_b = await ensure_project_library(session, pid_b)

        # keep：A 库 scored（无 wiki、无全文分段）
        keep = await add_paper(
            session,
            project_id=project_id,
            title="Sparse Attention Methods",
            year=2025,
            arxiv_id="2501.00001",
            dedup_key="arxiv:2501.00001",
            status="scored",
            relevance_score=0.8,
        )
        # drop：A 库 compiled（有 wiki）+ B 库成员 + 分段 + 概念 + 笔记划线 + 个人视角
        drop = await add_paper(
            session,
            project_id=project_id,
            title="Sparse Attention Methods (v2)",
            year=2025,
            doi="10.1000/sparse",
            dedup_key="doi:10.1000/sparse",
            status="compiled",
            relevance_score=0.9,
            wiki_content="# 解读\n讲 [[Attention]]。",
        )
        session.add(LibraryPaper(library_id=lib_b.id, paper_id=drop.id, status="candidate"))
        session.add(PaperChunk(paper_id=drop.id, seq=0, text="chunk one"))
        session.add(PaperChunk(paper_id=drop.id, seq=1, text="chunk two"))
        shared = await add_concept(
            session, project_id=project_id, name="Attention", slug="attention"
        )
        only_drop = await add_concept(
            session, project_id=project_id, name="Sparsity", slug="sparsity"
        )
        await session.execute(
            paper_concepts.insert(),
            [
                {"paper_id": keep.id, "concept_id": shared.id},
                {"paper_id": drop.id, "concept_id": shared.id},  # 两边都有 → 去重
                {"paper_id": drop.id, "concept_id": only_drop.id},  # 仅 drop → repoint
            ],
        )
        session.add(PaperNote(paper_id=drop.id, author_id=owner_id, content="note"))
        session.add(
            PaperHighlight(
                paper_id=drop.id,
                author_id=owner_id,
                page=1,
                rects=[{"x0": 0, "y0": 0, "x1": 1, "y1": 1}],
                selected_text="hi",
            )
        )
        # 个人视角冲突：keep 未读不星标，drop 已读且星标 → 合并取并/更靠后
        session.add(PaperUserMeta(paper_id=keep.id, user_id=owner_id, starred=False))
        session.add(
            PaperUserMeta(
                paper_id=drop.id, user_id=owner_id, starred=True, reading_status="read"
            )
        )
        # 另一用户只有 drop 行 → repoint
        session.add(PaperUserMeta(paper_id=drop.id, user_id=other_id, starred=True))
        # 书架冲突：同课题两行（keep 行无快照）
        session.add(TopicPaper(topic_id=pid, paper_id=keep.id))
        session.add(
            TopicPaper(topic_id=pid, paper_id=drop.id, wiki_snapshot="snap", note="why")
        )
        session.add(TopicPaper(topic_id=pid_b, paper_id=drop.id))  # 仅 drop → repoint
        # 软引用
        session.add(
            UserLibraryEntry(
                user_id=owner_id,
                dedup_key="doi:10.1000/sparse",
                title=drop.title,
                last_paper_id=drop.id,
            )
        )
        session.add(
            UserPublication(
                user_id=owner_id,
                dedup_key="doi:10.1000/sparse",
                title=drop.title,
                source="manual",
                paper_id=drop.id,
            )
        )
        await session.commit()
        keep_id, drop_id = keep.id, drop.id
        lib_a_id, lib_b_id = lib_a.id, lib_b.id
        shared_id, only_drop_id = shared.id, only_drop.id

    async with get_sessionmaker()() as session:
        report = await merge_service.merge_papers(session, keep_id=keep_id, drop_id=drop_id)

    assert report["dropped_dedup_key"] == "doi:10.1000/sparse"
    assert report["library_memberships"] == {"repointed": 1, "merged": 1}
    assert report["topic_papers"] == {"repointed": 1, "merged": 1}
    assert report["paper_user_meta"] == {"repointed": 1, "merged": 1}
    assert report["notes_repointed"] == 1
    assert report["highlights_repointed"] == 1
    assert report["concept_links"] == {"repointed": 1, "deduped": 1}
    assert report["chunks_moved"] == 2
    assert report["library_entries_repointed"] == 1
    assert report["publications_repointed"] == 1
    assert "doi" in report["fields_filled"]

    async with get_sessionmaker()() as session:
        assert await session.get(Paper, drop_id) is None
        keep = await session.get(Paper, keep_id)
        assert keep.doi == "10.1000/sparse"  # 缺项回填
        assert keep.arxiv_id == "2501.00001"  # keep 原值不被覆盖
        # A 库成员行合并：wiki 补上、状态升为 compiled、分数保留 keep 原值
        member_a = (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == lib_a_id, LibraryPaper.paper_id == keep_id
                )
            )
        ).scalar_one()
        assert member_a.status == "compiled"
        assert member_a.wiki_content and "[[Attention]]" in member_a.wiki_content
        assert member_a.relevance_score == 0.8
        # B 库成员行 repoint 到 keep
        member_b = (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == lib_b_id, LibraryPaper.paper_id == keep_id
                )
            )
        ).scalar_one()
        assert member_b.status == "candidate"
        # 个人视角合并
        owner_id = await _user_id(session, "merge-owner@example.com")
        meta = (
            await session.execute(
                select(PaperUserMeta).where(
                    PaperUserMeta.paper_id == keep_id, PaperUserMeta.user_id == owner_id
                )
            )
        ).scalar_one()
        assert meta.starred is True
        assert meta.reading_status == "read"
        # 概念链：shared 只剩一条、only_drop 已 repoint
        links = (
            await session.execute(
                select(paper_concepts.c.concept_id).where(paper_concepts.c.paper_id == keep_id)
            )
        ).scalars().all()
        assert sorted(map(str, links)) == sorted(map(str, [shared_id, only_drop_id]))
        # 分段随合并迁移
        chunk_count = len(
            (
                await session.execute(
                    select(PaperChunk.id).where(PaperChunk.paper_id == keep_id)
                )
            ).all()
        )
        assert chunk_count == 2
        # 书架：keep 行补了快照与备注
        shelf = (
            await session.execute(
                select(TopicPaper).where(TopicPaper.paper_id == keep_id)
            )
        ).scalars().all()
        assert len(shelf) == 2
        merged_row = next(t for t in shelf if t.topic_id == uuid.UUID(project_id))
        assert merged_row.wiki_snapshot == "snap"
        assert merged_row.note == "why"
        # 软引用 repoint
        entry = (
            (await session.execute(select(UserLibraryEntry))).scalars().first()
        )
        assert entry.last_paper_id == keep_id
        pub = (await session.execute(select(UserPublication))).scalars().first()
        assert pub.paper_id == keep_id

        # 幂等：drop 已不存在 → ValueError
        try:
            await merge_service.merge_papers(session, keep_id=keep_id, drop_id=drop_id)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


async def test_duplicate_candidates_and_merge_api(client):
    headers, project_id = await _setup(client, email="cand-owner@example.com")
    stranger_token = await register_and_login(client, email="cand-stranger@example.com")
    stranger = {"Authorization": f"Bearer {stranger_token}"}

    async with get_sessionmaker()() as session:
        keep = await add_paper(
            session,
            project_id=project_id,
            title="A Survey of LLM Agents",
            year=2025,
            status="compiled",
            wiki_content="# wiki",
        )
        drop = await add_paper(
            session,
            project_id=project_id,
            title="a survey of  llm-agents",  # 规范化后同标题
            year=2025,
            status="scored",
        )
        await add_paper(session, project_id=project_id, title="Unrelated Paper", status="scored")
        await session.commit()
        keep_id, drop_id = str(keep.id), str(drop.id)
        library_id = str((await get_library_for_project(session, uuid.UUID(project_id))).id)

    # 候选发现：无关用户 403；可管理者拿到一组（首行 = 有 wiki 的建议保留行）
    resp = await client.get(f"/api/libraries/{library_id}/duplicate-candidates", headers=stranger)
    assert resp.status_code == 403
    resp = await client.get(f"/api/libraries/{library_id}/duplicate-candidates", headers=headers)
    assert resp.status_code == 200, resp.text
    groups = resp.json()
    assert len(groups) == 1
    assert groups[0]["reason"] == "title"
    assert [p["id"] for p in groups[0]["papers"]] == [keep_id, drop_id]
    assert groups[0]["papers"][0]["has_wiki"] is True

    # 合并：无关用户 403；可管理者成功；再次合并（drop 已删）→ 400
    body = {"keep_id": keep_id, "drop_id": drop_id}
    resp = await client.post("/api/papers/merge", json=body, headers=stranger)
    assert resp.status_code == 403
    resp = await client.post("/api/papers/merge", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["kept_id"] == keep_id
    assert result["details"]["library_memberships"] == {"repointed": 0, "merged": 1}
    resp = await client.post("/api/papers/merge", json=body, headers=headers)
    assert resp.status_code == 400
    # 合并后候选清空
    resp = await client.get(f"/api/libraries/{library_id}/duplicate-candidates", headers=headers)
    assert resp.json() == []
