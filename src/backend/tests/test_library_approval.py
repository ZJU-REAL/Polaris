"""P9b：文献库生命周期 —— 用户建库（pending）+ 管理员审批 + 状态门 + 可见性过滤。

- 任意登录用户可建库，新库 status=pending（仅配置，不触发抓取）；
- 管理员 approve → active、reject → rejected（带理由）；
- pending/rejected 库不能触发 ingest（409 LIBRARY_NOT_ACTIVE），审批激活后可抓；
- pending/rejected 库仅创建者 + admin 可见（列表过滤 + 详情 404）；
- 创建者可管理自己的 pending 库（can_manage / PATCH）。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.user import User
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


async def _create_pending(client, headers, name="用户建的库"):
    resp = await client.post(
        "/api/libraries",
        json={"name": name, "statement": "一句话方向陈述", "anchors": ["2401.00001"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    return body["id"]


async def test_user_creates_pending_library(client):
    await _hdr(client, "p9b-a1@example.com")  # 占位 admin
    user = await _hdr(client, "p9b-u1@example.com")
    lib_id = await _create_pending(client, user)
    async with get_sessionmaker()() as session:
        from app.models.library_direction import DirectionLibrary

        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        assert lib.status == "pending"
        assert lib.submitted_by is not None
        # 锚点只存 arxiv-id 列表
        assert lib.definition["anchor_papers"] == ["2401.00001"]


async def test_admin_approve_library(client):
    admin = await _hdr(client, "p9b-a2@example.com")
    await _promote_admin("p9b-a2@example.com")
    user = await _hdr(client, "p9b-u2@example.com")
    lib_id = await _create_pending(client, user)

    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert resp.json()["review_note"] is None


async def test_admin_reject_library_with_note(client):
    admin = await _hdr(client, "p9b-a3@example.com")
    await _promote_admin("p9b-a3@example.com")
    user = await _hdr(client, "p9b-u3@example.com")
    lib_id = await _create_pending(client, user)

    resp = await client.post(
        f"/api/libraries/{lib_id}/reject", json={"note": "范围太宽，请收窄"}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert resp.json()["review_note"] == "范围太宽，请收窄"


async def test_approve_reject_admin_only(client):
    await _hdr(client, "p9b-a4@example.com")  # 占位 admin
    user = await _hdr(client, "p9b-u4@example.com")
    lib_id = await _create_pending(client, user)
    # 创建者（非 admin）不能审批自己的库
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=user)
    assert resp.status_code == 403
    resp = await client.post(f"/api/libraries/{lib_id}/reject", json={}, headers=user)
    assert resp.status_code == 403


async def test_pending_library_cannot_ingest(client, queue_stub):
    admin = await _hdr(client, "p9b-a5@example.com")
    await _promote_admin("p9b-a5@example.com")
    lib_id = await _create_pending(client, admin)  # admin 建库同样落 pending
    # 未审批激活 → 抓取被拒
    resp = await client.post(
        f"/api/libraries/{lib_id}/ingest/run", json={"mode": "bootstrap"}, headers=admin
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "LIBRARY_NOT_ACTIVE"
    assert queue_stub.jobs == []

    # 审批激活后可以触发（入队 run_voyage）
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/libraries/{lib_id}/ingest/run", json={"mode": "bootstrap"}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    assert queue_stub.jobs, "审批激活后触发应入队"


async def test_rejected_library_cannot_ingest(client, queue_stub):
    admin = await _hdr(client, "p9b-a6@example.com")
    await _promote_admin("p9b-a6@example.com")
    lib_id = await _create_pending(client, admin)
    await client.post(f"/api/libraries/{lib_id}/reject", json={"note": "no"}, headers=admin)
    resp = await client.post(
        f"/api/libraries/{lib_id}/ingest/run", json={"mode": "bootstrap"}, headers=admin
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "LIBRARY_NOT_ACTIVE"
    assert queue_stub.jobs == []


async def test_pending_library_hidden_from_stranger(client):
    await _hdr(client, "p9b-a7@example.com")  # 占位 admin
    owner = await _hdr(client, "p9b-owner7@example.com")
    stranger = await _hdr(client, "p9b-stranger7@example.com")
    lib_id = await _create_pending(client, owner, name="私有待审批库")

    # 列表：创建者看得到，陌生人看不到
    resp = await client.get("/api/libraries", headers=owner)
    assert lib_id in {x["id"] for x in resp.json()}
    resp = await client.get("/api/libraries", headers=stranger)
    assert lib_id not in {x["id"] for x in resp.json()}

    # 详情：陌生人 404，创建者 200
    resp = await client.get(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 404
    resp = await client.get(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 200


async def test_admin_sees_others_pending(client):
    admin = await _hdr(client, "p9b-a8@example.com")
    await _promote_admin("p9b-a8@example.com")
    owner = await _hdr(client, "p9b-owner8@example.com")
    lib_id = await _create_pending(client, owner)

    resp = await client.get("/api/libraries", headers=admin)
    assert lib_id in {x["id"] for x in resp.json()}
    resp = await client.get(f"/api/libraries/{lib_id}", headers=admin)
    assert resp.status_code == 200


async def test_creator_can_manage_and_edit_own_pending(client):
    await _hdr(client, "p9b-a9@example.com")  # 占位 admin
    owner = await _hdr(client, "p9b-owner9@example.com")
    lib_id = await _create_pending(client, owner)

    resp = await client.get(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 200
    assert resp.json()["can_manage"] is True

    # 创建者可编辑自己的 pending 库配置（审批前调整）
    resp = await client.patch(
        f"/api/libraries/{lib_id}", json={"statement": "收窄后的方向"}, headers=owner
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["definition"]["statement"] == "收窄后的方向"


async def test_active_library_visible_to_all(client):
    admin = await _hdr(client, "p9b-a10@example.com")
    await _promote_admin("p9b-a10@example.com")
    owner = await _hdr(client, "p9b-owner10@example.com")
    stranger = await _hdr(client, "p9b-stranger10@example.com")
    lib_id = await _create_pending(client, owner, name="将被激活的库")
    await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)

    resp = await client.get("/api/libraries", headers=stranger)
    assert lib_id in {x["id"] for x in resp.json()}
    resp = await client.get(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
