"""P7 Step 1：课题 × 文献库关联 + 库生命周期独立。

- 建课题仍自动建起源库，但同时落一条 topic_source_libraries 关联行；
- 删课题不再级联删库：direction_libraries.project_id 置 NULL，library_papers/
  concepts/curators 全部保留，只有该课题自己的关联行随课题消失；
- DELETE /libraries/{id}（admin）：有课题关联默认 409，force=true 才删；论文
  内容池（papers 表）行永不受影响。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper, TopicSourceLibrary
from app.models.paper import Concept, Paper
from app.services import libraries as libraries_service
from tests.conftest import add_concept, add_paper, register_and_login


async def _register(client, email):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def test_create_project_associates_its_own_library(client):
    admin = await _register(client, "p7-admin1@example.com")
    resp = await client.post("/api/projects", json={"name": "P7 课题一"}, headers=admin)
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        library_ids = await libraries_service.get_source_library_ids(session, project_id)
        assert len(library_ids) == 1
        library = await libraries_service.get_library_for_project(session, project_id)
        assert library is not None
        assert library.id == library_ids[0]
        assert library.project_id == project_id


async def test_delete_project_keeps_library_and_content_orphans_association(client):
    admin = await _register(client, "p7-admin2@example.com")
    resp = await client.post("/api/projects", json={"name": "P7 课题二"}, headers=admin)
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        library = await libraries_service.get_library_for_project(session, project_id)
        assert library is not None
        library_id = library.id
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Survives Project Deletion",
            status="compiled",
            relevance_score=0.8,
            wiki_content="# 解读",
        )
        await add_concept(
            session, project_id=project_id, name="X", slug="x", definition="定义"
        )
        await session.commit()
        paper_id = paper.id

    resp = await client.delete(f"/api/projects/{project_id}", headers=admin)
    assert resp.status_code == 204, resp.text

    async with get_sessionmaker()() as session:
        # 库本体、成员行、概念都还在；只有 project_id 回指置空
        refreshed = await session.get(DirectionLibrary, library_id)
        assert refreshed is not None
        assert refreshed.project_id is None
        membership = (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == library_id, LibraryPaper.paper_id == paper_id
                )
            )
        ).scalar_one_or_none()
        assert membership is not None
        concept_count = (
            await session.execute(
                select(Concept).where(Concept.library_id == library_id)
            )
        ).scalars().all()
        assert len(concept_count) == 1
        # 论文内容池行本身也不动
        assert await session.get(Paper, paper_id) is not None
        # 课题自己的关联行随课题消失
        assoc = (
            await session.execute(
                select(TopicSourceLibrary).where(TopicSourceLibrary.topic_id == project_id)
            )
        ).scalar_one_or_none()
        assert assoc is None


async def test_delete_library_requires_force_when_topics_linked(client):
    admin = await _register(client, "p7-admin3@example.com")
    resp = await client.post("/api/projects", json={"name": "P7 课题三"}, headers=admin)
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    resp = await client.get("/api/libraries", headers=admin)
    library_id = next(x["id"] for x in resp.json() if x["project_id"] == project_id)

    resp = await client.delete(f"/api/libraries/{library_id}", headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "LIBRARY_HAS_TOPICS"

    resp = await client.delete(f"/api/libraries/{library_id}?force=true", headers=admin)
    assert resp.status_code == 204, resp.text
    resp = await client.get(f"/api/libraries/{library_id}", headers=admin)
    assert resp.status_code == 404


async def test_delete_library_non_admin_forbidden(client):
    await _register(client, "p7-admin4@example.com")  # 首个注册者=平台 admin，占位
    member = await _register(client, "p7-member4@example.com")
    resp = await client.post("/api/projects", json={"name": "P7 课题四"}, headers=member)
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    resp = await client.get("/api/libraries", headers=member)
    library_id = next(x["id"] for x in resp.json() if x["project_id"] == project_id)

    resp = await client.delete(f"/api/libraries/{library_id}", headers=member)
    assert resp.status_code == 403


async def test_delete_library_without_topics_needs_no_force(client):
    """独立库（无课题关联）删除不受 force 影响——为 Step 3 独立建库铺路的行为先行验证。"""
    admin = await _register(client, "p7-admin5@example.com")
    async with get_sessionmaker()() as session:
        library = DirectionLibrary(name="孤儿库", created_by=None, project_id=None)
        session.add(library)
        await session.commit()
        library_id = library.id

    resp = await client.delete(f"/api/libraries/{library_id}", headers=admin)
    assert resp.status_code == 204, resp.text


async def test_get_source_libraries_returns_multiple_associations(client):
    """手动多关联一个库：get_source_libraries 返回并集；get_library_for_project 仍优先起源库。"""
    admin = await _register(client, "p7-admin6@example.com")
    resp = await client.post("/api/projects", json={"name": "P7 课题六"}, headers=admin)
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        origin = await libraries_service.get_library_for_project(session, project_id)
        assert origin is not None
        extra = DirectionLibrary(name="额外方向库", created_by=None, project_id=None)
        session.add(extra)
        await session.flush()
        await libraries_service.set_source_libraries(
            session, topic_id=project_id, library_ids=[origin.id, extra.id]
        )
        await session.commit()
        extra_id = extra.id

        libraries = await libraries_service.get_source_libraries(session, project_id)
        assert {lib.id for lib in libraries} == {origin.id, extra_id}

        resolved = await libraries_service.get_library_for_project(session, project_id)
        assert resolved is not None
        assert resolved.id == origin.id  # 起源库优先，即便有多个关联
