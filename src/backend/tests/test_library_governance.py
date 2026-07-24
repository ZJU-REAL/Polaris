"""P6/P8a 库治理：库级写权限助手、策展人任命（仅 admin）、库定义编辑（收录配置权威源）。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, DirectionLibraryCurator
from app.models.project import Project
from app.models.user import User
from app.services import libraries as libraries_service
from tests.conftest import make_project_with_library, register_and_login


async def _register(client, email):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _user_id_of(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (await session.execute(select(User.id).where(User.email == email))).scalar_one()


async def _setup(client):
    """第一个注册者自动成为平台 admin；owner 建课题（隐式库）。"""
    admin = await _register(client, "gov-admin@example.com")
    owner = await _register(client, "gov-owner@example.com")
    curator = await _register(client, "gov-curator@example.com")
    stranger = await _register(client, "gov-stranger@example.com")
    # P9c：课题不再自动建库——显式建课题 + 关联一条 active 起源库（project_id 回指）。
    project_id, library_id = await make_project_with_library(client, owner, name="治理方向")
    return admin, owner, curator, stranger, project_id, str(library_id)


async def _appoint(client, admin, library_id, email) -> uuid.UUID:
    user_id = await _user_id_of(email)
    resp = await client.put(
        f"/api/libraries/{library_id}/curators",
        json={"user_ids": [str(user_id)]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    return user_id


async def test_can_manage_library_three_identities(client):
    _admin, _owner, _curator, _stranger, project_id, library_id = await _setup(client)
    curator_id = await _user_id_of("gov-curator@example.com")
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        admin_user = (
            await session.execute(select(User).where(User.email == "gov-admin@example.com"))
        ).scalar_one()
        owner_user = (
            await session.execute(select(User).where(User.email == "gov-owner@example.com"))
        ).scalar_one()
        stranger_user = (
            await session.execute(select(User).where(User.email == "gov-stranger@example.com"))
        ).scalar_one()
        curator_user = await session.get(User, curator_id)

        # 平台 admin / 背后课题成员可管理；无关用户不可
        assert await libraries_service.can_manage_library(session, user=admin_user, library=library)
        assert await libraries_service.can_manage_library(session, user=owner_user, library=library)
        assert not await libraries_service.can_manage_library(
            session, user=stranger_user, library=library
        )
        # 任命为策展人后可管理
        assert not await libraries_service.can_manage_library(
            session, user=curator_user, library=library
        )
        session.add(DirectionLibraryCurator(library_id=library.id, user_id=curator_id))
        await session.commit()
        assert await libraries_service.can_manage_library(
            session, user=curator_user, library=library
        )


async def test_curator_appointment_admin_only(client):
    admin, owner, curator, stranger, _project_id, library_id = await _setup(client)
    curator_id = await _user_id_of("gov-curator@example.com")

    # 非 admin（即使是课题 owner）不能任命
    resp = await client.put(
        f"/api/libraries/{library_id}/curators",
        json={"user_ids": [str(curator_id)]},
        headers=owner,
    )
    assert resp.status_code == 403
    # admin 全量替换名单
    resp = await client.put(
        f"/api/libraries/{library_id}/curators",
        json={"user_ids": [str(curator_id)]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert [row["user_id"] for row in resp.json()] == [str(curator_id)]
    # 未知 user_id → 400
    resp = await client.put(
        f"/api/libraries/{library_id}/curators",
        json={"user_ids": [str(uuid.uuid4())]},
        headers=admin,
    )
    assert resp.status_code == 400
    # 名单查看：可管理者（策展人本人）可见；无关用户 403
    resp = await client.get(f"/api/libraries/{library_id}/curators", headers=curator)
    assert resp.status_code == 200
    resp = await client.get(f"/api/libraries/{library_id}/curators", headers=stranger)
    assert resp.status_code == 403
    # 空名单替换 = 清空
    resp = await client.put(
        f"/api/libraries/{library_id}/curators", json={"user_ids": []}, headers=admin
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_patch_library_permission_and_definition_authority(client):
    admin, _owner, curator, stranger, project_id, library_id = await _setup(client)

    # 无关用户 403
    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"name": "hijack"}, headers=stranger
    )
    assert resp.status_code == 403

    await _appoint(client, admin, library_id, "gov-curator@example.com")
    resp = await client.patch(
        f"/api/libraries/{library_id}",
        json={
            "name": "稀疏注意力",
            "statement": "稀疏注意力机制的效率研究",
            "cadence": "daily",
            "monthly_budget": 500000,
            "rubric": ["和稀疏注意力直接相关"],
            "anchors": [{"arxiv_id": "2404.00001"}],
        },
        headers=curator,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "稀疏注意力"
    assert body["monthly_budget"] == 500000
    assert body["can_manage"] is True
    # 响应带出收录配置全量（供「收录设置」回填）
    assert body["definition"]["rubric"] == ["和稀疏注意力直接相关"]
    assert body["definition"]["anchor_papers"] == [{"arxiv_id": "2404.00001"}]

    # P8a：库是收录配置唯一权威源——写入 library.definition，不再写回起源课题
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        assert library.name == "稀疏注意力"
        assert library.monthly_budget == 500000
        assert library.statement == "稀疏注意力机制的效率研究"
        definition = library.definition
        assert definition["statement"] == "稀疏注意力机制的效率研究"
        assert definition["rubric"] == ["和稀疏注意力直接相关"]
        assert definition["anchor_papers"] == [{"arxiv_id": "2404.00001"}]
        assert definition["cadence"] == "daily"
        # 起源课题不再承载收录配置（P9e：project.definition 已退役）——库 patch 不外溢到课题
        project = await session.get(Project, uuid.UUID(project_id))
        assert project.statement != "稀疏注意力机制的效率研究"

    # 显式传 null 清空预算
    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"monthly_budget": None}, headers=curator
    )
    assert resp.status_code == 200
    assert resp.json()["monthly_budget"] is None


async def test_curator_and_admin_can_use_project_paper_endpoints(client):
    admin, _owner, curator, stranger, project_id, library_id = await _setup(client)
    # 无关用户：project 作用域文献端点视为不存在
    resp = await client.get(f"/api/projects/{project_id}/papers", headers=stranger)
    assert resp.status_code == 404
    # 策展人（非成员）：与成员同权
    await _appoint(client, admin, library_id, "gov-curator@example.com")
    resp = await client.get(f"/api/projects/{project_id}/papers", headers=curator)
    assert resp.status_code == 200, resp.text
    # 平台 admin：同样放行
    resp = await client.get(f"/api/projects/{project_id}/papers", headers=admin)
    assert resp.status_code == 200, resp.text
    # 库列表里策展人 can_manage=True、is_mine=False
    resp = await client.get("/api/libraries", headers=curator)
    row = next(x for x in resp.json() if x["id"] == library_id)
    assert row["can_manage"] is True
    assert row["is_mine"] is False
