from tests.conftest import add_paper, register_and_login


async def test_projects_require_auth(client):
    resp = await client.get("/api/projects")
    assert resp.status_code == 401


async def test_create_and_list_projects(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/projects",
        json={"name": "LLM 推理加速", "definition": {"topic": "inference"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    project = resp.json()
    assert project["name"] == "LLM 推理加速"
    assert project["slug"]  # 自动生成
    assert project["status"] == "active"
    assert project["definition"] == {"topic": "inference"}

    resp = await client.post(
        "/api/projects", json={"name": "Second", "slug": "second"}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "second"

    resp = await client.get("/api/projects", headers=headers)
    assert resp.status_code == 200
    slugs = {p["slug"] for p in resp.json()}
    assert {project["slug"], "second"} <= slugs

    # get by id
    resp = await client.get(f"/api/projects/{project['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == project["id"]


async def test_delete_project(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "to-delete"}, headers=headers)
    project_id = resp.json()["id"]

    # P7：删课题保留库与内容池，只解除关联（下方断言）
    import uuid as _uuid

    from sqlalchemy import func, select

    from app.core.db import get_sessionmaker
    from app.models.paper import Paper

    async with get_sessionmaker()() as session:
        session.add(await add_paper(
            session,
            project_id=_uuid.UUID(project_id),
            title="orphan check"),
        )
        await session.commit()

    resp = await client.delete(f"/api/projects/{project_id}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/projects/{project_id}", headers=headers)
    assert resp.status_code == 404
    async with get_sessionmaker()() as session:
        from app.models.library_direction import (
            DirectionLibrary,
            LibraryPaper,
            TopicSourceLibrary,
        )

        # P7：删课题不再删库——起源库 project_id 置 NULL、成员行与内容池 Paper 都保留；
        # 只有课题自己的关联行随课题消失（library 变孤儿，admin 可后续删库）。
        libs = (
            await session.execute(
                select(func.count()).where(
                    DirectionLibrary.project_id == _uuid.UUID(project_id)
                )
            )
        ).scalar_one()
        assert libs == 0  # 无库仍回指这个已删课题
        total_libs = (
            await session.execute(select(func.count()).select_from(DirectionLibrary))
        ).scalar_one()
        assert total_libs == 1  # 库本体存活（project_id 已 SET NULL）
        memberships = (
            await session.execute(select(func.count()).select_from(LibraryPaper))
        ).scalar_one()
        assert memberships == 1  # 成员行随库存活
        assoc = (
            await session.execute(
                select(func.count()).where(
                    TopicSourceLibrary.topic_id == _uuid.UUID(project_id)
                )
            )
        ).scalar_one()
        assert assoc == 0  # 课题自己的关联行随课题删除
        pool = (await session.execute(select(func.count()).select_from(Paper))).scalar_one()
        assert pool == 1


async def test_delete_project_requires_owner(client):
    token_a = await register_and_login(client, email="owner@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    resp = await client.post("/api/projects", json={"name": "guarded"}, headers=headers_a)
    project_id = resp.json()["id"]

    # 普通成员：403；非成员：404
    token_b = await register_and_login(client, email="member@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    await client.post(
        "/api/projects/" + project_id + "/members",
        json={"email": "member@example.com", "role": "member"},
        headers=headers_a,
    )
    resp = await client.delete(f"/api/projects/{project_id}", headers=headers_b)
    assert resp.status_code == 403

    token_c = await register_and_login(client, email="stranger@example.com")
    resp = await client.delete(
        f"/api/projects/{project_id}", headers={"Authorization": f"Bearer {token_c}"}
    )
    assert resp.status_code == 404

    # owner 可删
    resp = await client.delete(f"/api/projects/{project_id}", headers=headers_a)
    assert resp.status_code == 204


async def test_get_project_not_member_404(client):
    token_a = await register_and_login(client, email="a@example.com")
    resp = await client.post(
        "/api/projects",
        json={"name": "private"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    project_id = resp.json()["id"]

    token_b = await register_and_login(client, email="b@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404

    # 非成员列表为空
    resp = await client.get("/api/projects", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.json() == []
