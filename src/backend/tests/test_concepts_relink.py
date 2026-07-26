"""全库概念补建（POST /projects/{project_id}/concepts/relink）与概念同步清理。"""

import uuid

from sqlalchemy import insert, select

from app.core.db import get_sessionmaker
from app.models.paper import Paper, PaperWiki, paper_concepts
from app.services.concepts import link_paper_concepts, placeholder_definition

from .conftest import (
    add_concept,
    add_paper,
    membership_of,
    project_concepts,
    register_and_login,
    wiki_of,
)


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "relink-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    pid = uuid.UUID(project_id)

    async with get_sessionmaker()() as session:
        session.add_all(
            [
                await add_paper(session,
                    project_id=pid,
                    title="Paper A",
                    status="compiled",
                    wiki_content="本文提出 [[自我博弈]]，结合 [[强化学习]] 训练。",
                ),
                await add_paper(session,
                    project_id=pid,
                    title="Paper B",
                    status="included",
                    wiki_content="基于 [[强化学习]] 与 [[课程学习]] 的方法。",
                ),
                # 未编译 / 无 wiki 内容的不参与
                await add_paper(session, project_id=pid, title="Paper C", status="candidate"),
            ]
        )
        await session.commit()
    return project_id, headers


async def test_relink_creates_concepts_and_links(client):
    project_id, headers = await _setup(client)

    resp = await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["papers"] == 2
    assert body["concepts_created"] == 3
    assert body["links_created"] == 4  # A:2 + B:2（强化学习共享一个概念）
    # 转正门槛：只有被两篇论文都提到的「强化学习」进概念库，另外两个留在候选
    assert body["concepts_promoted"] == 1
    assert body["promoted_concepts"] == ["强化学习"]

    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    counts = {c["name"]: c["paper_count"] for c in resp.json()}
    assert counts == {"强化学习": 2}


async def test_relink_is_idempotent(client):
    project_id, headers = await _setup(client)
    await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)

    resp = await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["concepts_created"] == 0
    assert body["links_created"] == 0


async def test_relink_backfills_placeholder_definitions(client):
    # 此前批量截断/失败留下的占位概念（正文里仍在引用），手动补建时应重新拿到定义并更正类别
    project_id, headers = await _setup(client)
    pid = uuid.UUID(project_id)
    async with get_sessionmaker()() as session:
        session.add_all(
            [
                await add_concept(session,
                    project_id=pid,
                    name="自我博弈",
                    slug="old-x",
                    definition=placeholder_definition("自我博弈"),
                    category="other",
                ),
                await add_concept(session,
                    project_id=pid,
                    name="课程学习",
                    slug="old-y",
                    definition=placeholder_definition("课程学习"),
                    category="other",
                ),
            ]
        )
        await session.commit()

    resp = await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["concepts_backfilled"] == 2

    async with get_sessionmaker()() as session:
        rows = [
            c
            for c in await project_concepts(session, project_id=pid)
            if c.name in ("自我博弈", "课程学习")
        ]
        assert len(rows) == 2
        for concept in rows:
            assert not concept.definition.endswith("（定义待补充）")
            assert concept.category == "method"  # fake provider 返回 method


async def _set_wiki_content(project_id: str, title: str, content) -> uuid.UUID:
    """改写这篇论文的解读正文（论文级唯一一份；content=None 表示删掉解读）。"""
    async with get_sessionmaker()() as session:
        paper = (
            await session.execute(select(Paper).where(Paper.title == title))
        ).scalar_one()
        wiki = await wiki_of(session, paper_id=paper.id)
        if content is None:
            if wiki is not None:
                await session.delete(wiki)
        elif wiki is None:
            session.add(PaperWiki(paper_id=paper.id, content=content))
        else:
            wiki.content = content
        await session.commit()
        return paper.id


async def _concept_counts(client, project_id: str, headers) -> dict[str, int]:
    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    return {c["name"]: c["paper_count"] for c in resp.json()}


async def test_relink_removes_stale_links_and_orphan_concepts(client):
    # 重编译改写正文后重跑 relink：陈旧关联删除，共享概念保留，独占概念删词条
    project_id, headers = await _setup(client)
    await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)

    # Paper A 正文改写：不再引用「自我博弈」，只剩「强化学习」
    await _set_wiki_content(project_id, "Paper A", "改写后只讨论 [[强化学习]]。")

    resp = await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["links_removed"] == 1  # A-自我博弈
    assert body["concepts_removed"] == 1  # 自我博弈已无任何论文引用
    assert body["concepts_created"] == 0 and body["links_created"] == 0

    counts = await _concept_counts(client, project_id, headers)
    assert counts == {"强化学习": 2}  # 课程学习只有 B 一篇 → 仍是候选，不可见


async def test_relink_keeps_concepts_referenced_by_trash_papers(client):
    # 回收站（excluded）论文的引用也算数：只删真正零引用的概念
    project_id, headers = await _setup(client)
    pid = uuid.UUID(project_id)
    async with get_sessionmaker()() as session:
        trashed = await add_paper(session, project_id=pid, title="Trashed", status="excluded")
        kept = await add_concept(
            session,
            project_id=pid,
            name="回收站概念",
            slug="trash-c",
            definition="d",
        )
        orphan = await add_concept(
            session,
            project_id=pid,
            name="孤儿概念",
            slug="orphan-c",
            definition="d",
        )
        session.add_all([trashed, kept, orphan])
        await session.flush()
        await session.execute(
            insert(paper_concepts).values(paper_id=trashed.id, concept_id=kept.id)
        )
        await session.commit()

    resp = await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["concepts_removed"] == 1  # 只删孤儿概念

    async with get_sessionmaker()() as session:
        names = [c.name for c in await project_concepts(session, project_id=pid)]
    assert "回收站概念" in names and "孤儿概念" not in names


async def test_link_paper_concepts_syncs_after_recompile(client):
    # 单篇同步语义：重编译换正文后，陈旧关联删除、独占概念删词条、共享概念保留
    project_id, headers = await _setup(client)
    await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)

    paper_id = await _set_wiki_content(
        project_id, "Paper A", "新解读引入 [[新概念]]，仍基于 [[强化学习]]。"
    )
    async with get_sessionmaker()() as session:
        paper = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one()
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        created, linked = await link_paper_concepts(session, paper, membership)
    assert created == 1 and linked == 1  # 新概念（llm=None → 占位定义）

    counts = await _concept_counts(client, project_id, headers)
    # 自我博弈只被 A 引用过 → 词条删除；强化学习仍被 A/B 共享而可见；
    # 新概念/课程学习各只有一篇引用 → 候选，不进概念库
    assert counts == {"强化学习": 2}


async def test_link_paper_concepts_empty_content_keeps_links(client):
    # 正文为空/None 时不做同步删除，防止误删全部关联
    project_id, headers = await _setup(client)
    await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)

    paper_id = await _set_wiki_content(project_id, "Paper A", None)
    async with get_sessionmaker()() as session:
        paper = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one()
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        assert await link_paper_concepts(session, paper, membership) == (0, 0)

    counts = await _concept_counts(client, project_id, headers)
    assert counts == {"强化学习": 2}  # 单篇引用的两个仍是候选


async def test_link_paper_concepts_without_membership(client):
    """池内论文（不属于任何库）也能上链：不传成员行 → 记账落平台级，不报错。

    定义调用发生在转正那一刻，所以造两篇都提到同一个概念的池内论文。"""
    from app.core.llm.router import LLMRouter
    from app.models.llm_config import LLMUsage
    from app.models.paper import Concept, new_paper

    await register_and_login(client, email="pool-link@example.com")
    paper_ids = []
    for i in range(2):
        async with get_sessionmaker()() as session:
            paper = new_paper(title=f"Pool Paper {i}", abstract="a", source="manual")
            session.add(paper)
            await session.flush()
            paper.wiki = PaperWiki(content="本文用 [[池内概念]] 做实验。")
            await session.commit()
            created, linked = await link_paper_concepts(session, paper, llm=LLMRouter())
            paper_ids.append(paper.id)
        assert (created, linked) == (1 if i == 0 else 0, 1)

    async with get_sessionmaker()() as session:
        names = (
            (
                await session.execute(
                    select(Concept.name)
                    .join(paper_concepts, paper_concepts.c.concept_id == Concept.id)
                    .where(paper_concepts.c.paper_id == paper_ids[0])
                )
            )
            .scalars()
            .all()
        )
        assert names == ["池内概念"]
        # 第一篇只建候选、不调 LLM；第二篇让它转正，这时才有定义调用（记平台级）
        usage = (await session.execute(select(LLMUsage))).scalars().all()
        assert usage and all(row.library_id is None for row in usage)
        concept = (
            await session.execute(select(Concept).where(Concept.name == "池内概念"))
        ).scalar_one()
        assert concept.status == "active"
        assert concept.definition == "池内概念 的一句话定义（fake）"


async def test_relink_requires_membership(client):
    project_id, _ = await _setup(client)
    other = await register_and_login(client, email="bob@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/concepts/relink",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


async def test_auto_sweep_backfills_placeholders_capped(client):
    # voyage 自动上链（backfill=False）也应做有上限的占位回填（偶发失败自愈）
    from app.core.llm.router import LLMRouter
    from app.services.concepts import link_all_paper_concepts, placeholder_definition

    project_id, headers = await _setup(client)
    pid = uuid.UUID(project_id)
    # 现实场景：占位概念被论文正文引用（「强化学习」在 _setup 两篇论文的 wiki 里）。
    # 无引用的占位会被 #65 的孤儿清理直接删除，不走回填。
    async with get_sessionmaker()() as session:
        session.add(
            await add_concept(session,
                project_id=pid,
                name="强化学习",
                slug="ph-rl",
                definition=placeholder_definition("强化学习"),
                category="other",
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        from app.services.libraries import get_library_for_project

        library = await get_library_for_project(session, pid)
        stats, _ = await link_all_paper_concepts(
            session, library_id=library.id, llm=LLMRouter(), backfill=False
        )
    assert stats["concepts_backfilled"] == 1

    async with get_sessionmaker()() as session:
        row = next(
            c
            for c in await project_concepts(session, project_id=pid)
            if c.name == "强化学习"
        )
        assert not row.definition.endswith("（定义待补充）")
        assert row.category == "method"  # fake provider 返回 method
