"""全库概念补建：POST /projects/{project_id}/concepts/relink。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Concept, Paper
from app.services.concepts import placeholder_definition

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
    # 此前批量截断/失败留下的占位概念，手动补建时应重新拿到定义并更正类别
    project_id, headers = await _setup(client)
    pid = uuid.UUID(project_id)
    async with get_sessionmaker()() as session:
        session.add_all(
            [
                Concept(
                    project_id=pid,
                    name="旧概念X",
                    slug="old-x",
                    definition=placeholder_definition("旧概念X"),
                    category="other",
                ),
                Concept(
                    project_id=pid,
                    name="旧概念Y",
                    slug="old-y",
                    definition=placeholder_definition("旧概念Y"),
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
                        Concept.project_id == pid, Concept.name.in_(["旧概念X", "旧概念Y"])
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


async def test_relink_requires_membership(client):
    project_id, _ = await _setup(client)
    other = await register_and_login(client, email="bob@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/concepts/relink",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404
