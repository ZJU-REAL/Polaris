"""解耦后的库鉴权口径（PR-3）：库工作台一律走 /libraries/{id}/*，判断
「你是不是这个库的策展人（或平台 admin）」，不再是「你是不是起源课题的成员」。

这两个口径认的不是同一批人 —— 历史库上靠课题成员身份在管库的人，切换后
会失去管理权。迁移 b3d81f6c05a9 把他们补成策展人来兜住。本文件锁住这个
前后关系：课题成员身份本身不给管理权，补成策展人才给。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, DirectionLibraryCurator
from app.models.project import ProjectMember
from app.models.user import User
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one().id


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


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


async def _add_topic_member(project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ProjectMember(project_id=project_id, user_id=user_id, role="member")
        )
        await session.commit()


async def _add_curator(lib_id: str, user_id: uuid.UUID) -> None:
    """迁移 b3d81f6c05a9 对每个历史库成员做的事。"""
    async with get_sessionmaker()() as session:
        session.add(
            DirectionLibraryCurator(library_id=uuid.UUID(lib_id), user_id=user_id)
        )
        await session.commit()


async def _legacy_library(client, *, prefix):
    """造一个「有起源课题」的历史库，返回 (owner_hdr, admin_hdr, lib_id, project_id)。"""
    admin_email = f"{prefix}-admin@example.com"
    admin = await _hdr(client, admin_email)
    await _promote_admin(admin_email)

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
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    assert resp.status_code == 200, resp.text

    await _set_origin_topic(lib_id, project_id)
    return owner, admin, lib_id, project_id


async def test_topic_membership_alone_does_not_grant_library_manage(client):
    """课题成员身份不再等于库管理权 —— 这就是回填存在的理由。"""
    _owner, _admin, lib_id, project_id = await _legacy_library(client, prefix="authz-member")

    member_email = "authz-member-teammate@example.com"
    member = await _hdr(client, member_email)
    member_id = await _user_id(member_email)
    # 他是这个库起源课题的正式成员 —— 解耦前正是靠这重身份在管库
    await _add_topic_member(project_id, member_id)

    # 但课题成员身份不给库管理权：管理端点拒绝
    resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=member)
    assert resp.status_code == 403, resp.text
    resp = await client.post(f"/api/libraries/{lib_id}/concepts/relink", headers=member)
    assert resp.status_code == 403, resp.text

    # 补成策展人（= 迁移做的事）后放行
    await _add_curator(lib_id, member_id)
    resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=member)
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/libraries/{lib_id}/concepts/relink", headers=member)
    assert resp.status_code == 200, resp.text


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


async def test_platform_admin_manages_legacy_library_without_backfill(client):
    """平台 admin 本来就放行，不依赖回填 —— 本地这份数据回填是空操作的原因。"""
    _owner, admin, lib_id, _project_id = await _legacy_library(client, prefix="authz-admin")

    resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=admin)
    assert resp.status_code == 200, resp.text
