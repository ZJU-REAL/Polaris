"""解耦后的库鉴权口径（PR-3）：库工作台一律走 /libraries/{id}/*，判断
「你是不是这个库的创建者」，不再是「你是不是起源课题的相关人」（admin 旁路
已随 #614 移除，课题成员机制已随 #625 移除）。本文件锁住：与起源课题的任何
历史牵连都不给库管理权，管理权只看创建者。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _set_origin_topic(lib_id: str, project_id: uuid.UUID) -> None:
    """把库挂成「某课题建的」——即迁移前的历史库形态。"""
    async with get_sessionmaker()() as session:
        lib = (
            await session.execute(
                select(DirectionLibrary).where(DirectionLibrary.id == uuid.UUID(lib_id))
            )
        ).scalar_one()
        lib.project_id = project_id
        await session.commit()


async def _legacy_library(client, *, prefix):
    """造一个「有起源课题」的历史库，返回 (owner_hdr, admin_hdr, lib_id, project_id)。"""
    admin_email = f"{prefix}-admin@example.com"
    admin = await _hdr(client, admin_email)

    owner = await _hdr(client, f"{prefix}-owner@example.com")
    resp = await client.post(
        "/api/projects",
        json={"name": f"{prefix} 课题", "statement": "解耦前建的课题"},
        headers=owner,
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])

    resp = await client.post(
        "/api/libraries",
        json={"name": f"{prefix} 历史库", "statement": "当年跟着课题一起建的"},
        headers=owner,
    )
    assert resp.status_code == 201, resp.text
    lib_id = resp.json()["id"]
    # 转公共审批流已移除：直接把库置为公共（模拟存量公共历史库）
    async with get_sessionmaker()() as session:
        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        lib.is_public = True
        await session.commit()

    await _set_origin_topic(lib_id, project_id)
    return owner, admin, lib_id, project_id


async def test_topic_ties_alone_do_not_grant_library_manage(client):
    """与起源课题的历史牵连不给库管理权：只有创建者能管（#614/#625）。"""
    _owner, _admin, lib_id, _project_id = await _legacy_library(client, prefix="authz-member")

    # 解耦前靠课题成员身份在管库的人，如今只是一个普通用户（成员表已删，#625）
    member = await _hdr(client, "authz-member-teammate@example.com")

    # 非创建者：管理端点拒绝
    resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=member)
    assert resp.status_code == 403, resp.text
    resp = await client.post(f"/api/libraries/{lib_id}/concepts/relink", headers=member)
    assert resp.status_code == 403, resp.text

    # 库创建者本人照常放行（管理权 = 创建者，#614）
    owner_resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=_owner)
    assert owner_resp.status_code == 200, owner_resp.text


async def test_legacy_library_serves_library_scoped_endpoints(client):
    """有起源课题的库，集合级端点走库作用域一样能用（前端切过去的前提）。"""
    owner, _admin, lib_id, _project_id = await _legacy_library(client, prefix="authz-scope")

    for path in (
        f"/api/libraries/{lib_id}/papers",
        f"/api/libraries/{lib_id}/concepts",
        f"/api/libraries/{lib_id}/ingest/state",
        f"/api/libraries/{lib_id}/notes",
    ):
        resp = await client.get(path, headers=owner)
        assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text}"

