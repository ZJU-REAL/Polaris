"""P7 Step 2/3：课题关联库并集读取 + 独立建库 + 关联读写 API。

- 课题关联多个库 → 论文列表/检索是关联库论文的并集（跨库同一论文归并成一行）；
- 关联清空 → 语料为空，消费端返回空态而非报错；
- POST /libraries（admin）独立建库，project_id 恒为 NULL；
- GET/PUT /projects/{id}/source-libraries 读写课题关联。
"""

import uuid

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.services import libraries as libraries_service
from app.services import papers as papers_service
from tests.conftest import add_paper, register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _extra_library_with_paper(session, *, name, title, score=0.5):
    """独立库 + 一篇库内论文（compiled + wiki），返回 (library, paper)。"""
    lib = DirectionLibrary(name=name, created_by=None, project_id=None)
    session.add(lib)
    await session.flush()
    paper = Paper(title=title)
    session.add(paper)
    await session.flush()
    session.add(
        LibraryPaper(
            library_id=lib.id,
            paper_id=paper.id,
            status="compiled",
            relevance_score=score,
            wiki_content="# 解读",
        )
    )
    await session.flush()
    return lib, paper


async def test_list_papers_union_across_linked_libraries(client):
    admin = await _hdr(client, "p7u-1@example.com")
    resp = await client.post("/api/projects", json={"name": "并集课题"}, headers=admin)
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        await add_paper(
            session,
            project_id=project_id,
            title="Origin Paper",
            status="compiled",
            relevance_score=0.9,
            wiki_content="# o",
        )
        extra, _ = await _extra_library_with_paper(session, name="额外库", title="Extra Paper")
        origin = await libraries_service.get_library_for_project(session, project_id)
        await libraries_service.set_source_libraries(
            session, topic_id=project_id, library_ids=[origin.id, extra.id]
        )
        await session.commit()

        items, total = await papers_service.list_papers(
            session, project_id=project_id, status="library", size=50
        )
        titles = {v.paper.title for v in items}

    assert total == 2
    assert {"Origin Paper", "Extra Paper"} <= titles


async def test_list_papers_union_dedupes_shared_paper(client):
    """同一篇论文同时在两个关联库 → 并集列表只出现一次。"""
    admin = await _hdr(client, "p7u-2@example.com")
    resp = await client.post("/api/projects", json={"name": "去重课题"}, headers=admin)
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Shared Paper",
            status="compiled",
            relevance_score=0.6,
        )
        origin = await libraries_service.get_library_for_project(session, project_id)
        extra = DirectionLibrary(name="第二库", created_by=None, project_id=None)
        session.add(extra)
        await session.flush()
        # 同一篇论文在第二个库也有成员行（且带 wiki，视角应优先它）
        session.add(
            LibraryPaper(
                library_id=extra.id,
                paper_id=paper.id,
                status="compiled",
                relevance_score=0.95,
                wiki_content="# 更好的解读",
            )
        )
        await libraries_service.set_source_libraries(
            session, topic_id=project_id, library_ids=[origin.id, extra.id]
        )
        await session.commit()

        items, total = await papers_service.list_papers(
            session, project_id=project_id, status="library", size=50
        )

    assert total == 1
    assert items[0].paper.title == "Shared Paper"
    assert items[0].membership.wiki_content == "# 更好的解读"  # 有 wiki 的视角优先


async def test_empty_source_libraries_yields_empty_corpus(client):
    """关联清空 → list_papers 空、不报错。"""
    admin = await _hdr(client, "p7u-3@example.com")
    resp = await client.post("/api/projects", json={"name": "空语料课题"}, headers=admin)
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        await libraries_service.set_source_libraries(
            session, topic_id=project_id, library_ids=[]
        )
        await session.commit()
        items, total = await papers_service.list_papers(
            session, project_id=project_id, status="library"
        )

    assert items == []
    assert total == 0


async def test_create_library_admin_independent(client):
    admin = await _hdr(client, "p7u-4@example.com")
    resp = await client.post(
        "/api/libraries",
        json={"name": "独立库", "statement": "一句话", "cadence": "weekly"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "独立库"
    assert body["project_id"] is None
    assert body["is_mine"] is False
    # P10：新建库即刻可用的个人库（active + 非 public），无需审批
    assert body["status"] == "active"
    assert body["is_public"] is False


async def test_create_library_any_user_allowed(client):
    """P10：建库权限放开——任意登录用户可建，新库 active 个人库、创建者自动成为策展人。"""
    await _hdr(client, "p9b-admin5@example.com")  # 首个注册者=平台 admin，占位
    member = await _hdr(client, "p9b-member5@example.com")
    resp = await client.post("/api/libraries", json={"name": "x"}, headers=member)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["is_public"] is False
    assert body["can_manage"] is True  # 创建者可管理自己的个人库
    # 创建者被记为该库策展人
    resp = await client.get(f"/api/libraries/{body['id']}/curators", headers=member)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_source_libraries_read_write_api(client):
    admin = await _hdr(client, "p7u-6@example.com")
    resp = await client.post("/api/libraries", json={"name": "可关联库"}, headers=admin)
    lib_id = resp.json()["id"]
    resp = await client.post("/api/projects", json={"name": "关联课题"}, headers=admin)
    project_id = resp.json()["id"]

    # 全量替换关联为 [新独立库]（覆盖掉建课题时自动关联的起源库）
    resp = await client.put(
        f"/api/projects/{project_id}/source-libraries",
        json={"library_ids": [lib_id]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert {x["id"] for x in resp.json()} == {lib_id}

    resp = await client.get(f"/api/projects/{project_id}/source-libraries", headers=admin)
    assert resp.status_code == 200
    assert {x["id"] for x in resp.json()} == {lib_id}

    # 清空
    resp = await client.put(
        f"/api/projects/{project_id}/source-libraries",
        json={"library_ids": []},
        headers=admin,
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_source_libraries_requires_membership(client):
    """非课题成员不能读/写别人课题的关联库。"""
    owner = await _hdr(client, "p7u-owner7@example.com")
    resp = await client.post("/api/projects", json={"name": "私有课题"}, headers=owner)
    project_id = resp.json()["id"]
    outsider = await _hdr(client, "p7u-outsider7@example.com")
    resp = await client.get(f"/api/projects/{project_id}/source-libraries", headers=outsider)
    assert resp.status_code == 404
    resp = await client.put(
        f"/api/projects/{project_id}/source-libraries",
        json={"library_ids": []},
        headers=outsider,
    )
    assert resp.status_code == 404
