"""库治理：库级写权限助手（admin ∪ 创建者）与库定义编辑（收录配置权威源）。

策展人任命与转公共审批流已随实验室定位移除（P1 去实验室化）。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from app.models.project import Project
from app.models.user import User
from app.services import libraries as libraries_service
from tests.conftest import make_project_with_library, register_and_login


async def _register(client, email):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _setup(client):
    """第一个注册者自动成为平台 admin；owner 建课题（隐式库）。"""
    admin = await _register(client, "gov-admin@example.com")
    owner = await _register(client, "gov-owner@example.com")
    stranger = await _register(client, "gov-stranger@example.com")
    # P9c：课题不再自动建库——显式建课题 + 关联一条 active 起源库（project_id 回指）。
    project_id, library_id = await make_project_with_library(client, owner, name="治理方向")
    return admin, owner, stranger, project_id, str(library_id)


async def test_can_manage_library_identities(client):
    _admin, _owner, _stranger, _project_id, library_id = await _setup(client)
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

        # admin 旁路已随 role 移除（#614）：非创建者一律不可管
        assert not await libraries_service.can_manage_library(
            session, user=admin_user, library=library
        )
        assert await libraries_service.can_manage_library(session, user=owner_user, library=library)
        assert not await libraries_service.can_manage_library(
            session, user=stranger_user, library=library
        )
        # 批量版与逐库版规则一字不差
        assert libraries_service.can_manage_library_row(user=owner_user, library=library)
        assert not libraries_service.can_manage_library_row(user=stranger_user, library=library)


async def test_patch_library_permission_and_definition_authority(client):
    _admin, owner, stranger, project_id, library_id = await _setup(client)

    # 无关用户 403
    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"name": "hijack"}, headers=stranger
    )
    assert resp.status_code == 403

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
        headers=owner,
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
        # 起源课题不再承载收录配置（P9e）——库 patch 不外溢到课题
        project = await session.get(Project, uuid.UUID(project_id))
        assert project.statement != "稀疏注意力机制的效率研究"

    # 显式传 null 清空预算
    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"monthly_budget": None}, headers=owner
    )
    assert resp.status_code == 200
    assert resp.json()["monthly_budget"] is None


async def test_project_paper_endpoints_visibility(client):
    admin, owner, stranger, project_id, _library_id = await _setup(client)
    # 无关用户：project 作用域文献端点视为不存在
    resp = await client.get(f"/api/projects/{project_id}/papers", headers=stranger)
    assert resp.status_code == 404
    # 课题所有者放行；非成员（含此前的平台 admin）一律视为不存在（#614）
    resp = await client.get(f"/api/projects/{project_id}/papers", headers=owner)
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/projects/{project_id}/papers", headers=admin)
    assert resp.status_code == 404
