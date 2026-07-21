"""全库概念补建（POST /projects/{project_id}/concepts/relink）与概念同步清理。"""

import uuid

from sqlalchemy import insert, select

from app.core.db import get_sessionmaker
from app.models.paper import Concept, Paper, paper_concepts
from app.services.concepts import link_paper_concepts, placeholder_definition

from .conftest import register_and_login


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
                Paper(
                    project_id=pid,
                    title="Paper A",
                    status="compiled",
                    wiki_content="本文提出 [[自我博弈]]，结合 [[强化学习]] 训练。",
                ),
                Paper(
                    project_id=pid,
                    title="Paper B",
                    status="included",
                    wiki_content="基于 [[强化学习]] 与 [[课程学习]] 的方法。",
                ),
                # 未编译 / 无 wiki 内容的不参与
                Paper(project_id=pid, title="Paper C", status="candidate"),
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
    assert set(body["new_concepts"]) == {"自我博弈", "强化学习", "课程学习"}

    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    counts = {c["name"]: c["paper_count"] for c in resp.json()}
    assert counts == {"自我博弈": 1, "强化学习": 2, "课程学习": 1}


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
                Concept(
                    project_id=pid,
                    name="自我博弈",
                    slug="old-x",
                    definition=placeholder_definition("自我博弈"),
                    category="other",
                ),
                Concept(
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
        rows = (
            (
                await session.execute(
                    select(Concept).where(
                        Concept.project_id == pid, Concept.name.in_(["自我博弈", "课程学习"])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        for concept in rows:
            assert not concept.definition.endswith("（定义待补充）")
            assert concept.category == "method"  # fake provider 返回 method


async def _set_wiki_content(project_id: str, title: str, content) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        paper = (
            await session.execute(
                select(Paper).where(Paper.project_id == uuid.UUID(project_id), Paper.title == title)
            )
        ).scalar_one()
        paper.wiki_content = content
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
    assert counts == {"强化学习": 2, "课程学习": 1}


async def test_relink_keeps_concepts_referenced_by_trash_papers(client):
    # 回收站（excluded）论文的引用也算数：只删真正零引用的概念
    project_id, headers = await _setup(client)
    pid = uuid.UUID(project_id)
    async with get_sessionmaker()() as session:
        trashed = Paper(project_id=pid, title="Trashed", status="excluded")
        kept = Concept(project_id=pid, name="回收站概念", slug="trash-c", definition="d")
        orphan = Concept(project_id=pid, name="孤儿概念", slug="orphan-c", definition="d")
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
        names = (
            (await session.execute(select(Concept.name).where(Concept.project_id == pid)))
            .scalars()
            .all()
        )
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
        created, linked = await link_paper_concepts(session, paper)
    assert created == 1 and linked == 1  # 新概念（llm=None → 占位定义）

    counts = await _concept_counts(client, project_id, headers)
    # 自我博弈只被 A 引用过 → 词条删除；强化学习仍被 A/B 共享；课程学习仍挂 B
    assert counts == {"强化学习": 2, "课程学习": 1, "新概念": 1}


async def test_link_paper_concepts_empty_content_keeps_links(client):
    # 正文为空/None 时不做同步删除，防止误删全部关联
    project_id, headers = await _setup(client)
    await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)

    paper_id = await _set_wiki_content(project_id, "Paper A", None)
    async with get_sessionmaker()() as session:
        paper = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one()
        assert await link_paper_concepts(session, paper) == (0, 0)

    counts = await _concept_counts(client, project_id, headers)
    assert counts == {"自我博弈": 1, "强化学习": 2, "课程学习": 1}


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
            Concept(
                project_id=pid,
                name="强化学习",
                slug="ph-rl",
                definition=placeholder_definition("强化学习"),
                category="other",
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        stats, _ = await link_all_paper_concepts(
            session, project_id=pid, llm=LLMRouter(), backfill=False
        )
    assert stats["concepts_backfilled"] == 1

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(Concept).where(Concept.project_id == pid, Concept.name == "强化学习")
            )
        ).scalar_one()
        assert not row.definition.endswith("（定义待补充）")
        assert row.category == "method"  # fake provider 返回 method
