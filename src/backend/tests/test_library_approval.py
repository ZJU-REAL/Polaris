"""P10：文献库个人/公共归属 —— 建库即 active 个人库 + 申请转公共 + admin 审批。

- 任意登录用户建库 → 即刻可用的**个人库**（status=active、is_public=false），不审批；
- 个人库仅创建者 + admin 可见/可管理，token 记创建者账；
- 创建者/策展人经 POST /libraries/{id}/request-public 申请转公共 → status=pending；
- admin approve → is_public=true active（全实验室可见）；reject → 退回个人 active + 理由；
- 可见性：个人库对陌生人不可见（列表隐藏 + 详情 404），转公共后全员可见；
- 删除：个人库创建者本人或 admin 可删；公共库仅 admin 可删。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        from app.models.user import User

        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


async def _create_personal(client, headers, name="用户建的库"):
    resp = await client.post(
        "/api/libraries",
        json={"name": name, "statement": "一句话方向陈述", "anchors": ["2401.00001"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["is_public"] is False
    return body["id"]


# ---- 建库即 active 个人库 ----


async def test_user_creates_personal_active_library(client):
    await _hdr(client, "p10-a1@example.com")  # 占位 admin
    user = await _hdr(client, "p10-u1@example.com")
    lib_id = await _create_personal(client, user)
    async with get_sessionmaker()() as session:
        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        assert lib.status == "active"
        assert lib.is_public is False
        assert lib.submitted_by is not None
        assert lib.definition["anchor_papers"] == ["2401.00001"]


async def test_personal_library_can_ingest_without_approval(client, queue_stub):
    """P10：个人库即刻 active，无需审批即可触发抓取。"""
    admin = await _hdr(client, "p10-a2@example.com")
    await _promote_admin("p10-a2@example.com")
    lib_id = await _create_personal(client, admin)
    resp = await client.post(
        f"/api/libraries/{lib_id}/ingest/run", json={"mode": "bootstrap"}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    assert queue_stub.jobs, "个人 active 库触发应入队"


# ---- 申请转公共 + 审批 ----


async def test_request_public_sets_pending(client):
    await _hdr(client, "p10-a3@example.com")  # 占位 admin
    owner = await _hdr(client, "p10-owner3@example.com")
    lib_id = await _create_personal(client, owner)
    resp = await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["is_public"] is False  # 审批前仍是个人库


async def test_request_public_stranger_forbidden(client):
    await _hdr(client, "p10-a4@example.com")  # 占位 admin
    owner = await _hdr(client, "p10-owner4@example.com")
    stranger = await _hdr(client, "p10-stranger4@example.com")
    lib_id = await _create_personal(client, owner)
    # 陌生人看不到该个人库 → 申请转公共视为不存在（404）
    resp = await client.post(f"/api/libraries/{lib_id}/request-public", headers=stranger)
    assert resp.status_code == 404


async def test_curator_can_request_public(client):
    admin = await _hdr(client, "p10-a5@example.com")
    await _promote_admin("p10-a5@example.com")
    owner = await _hdr(client, "p10-owner5@example.com")
    curator = await _hdr(client, "p10-curator5@example.com")
    lib_id = await _create_personal(client, owner)
    # admin 把 curator 加为该库策展人
    async with get_sessionmaker()() as session:
        from app.models.user import User

        cur = (
            await session.execute(select(User).where(User.email == "p10-curator5@example.com"))
        ).scalar_one()
        cur_id = cur.id
    resp = await client.put(
        f"/api/libraries/{lib_id}/curators", json={"user_ids": [str(cur_id)]}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/libraries/{lib_id}/request-public", headers=curator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"


async def test_admin_approve_makes_public(client):
    admin = await _hdr(client, "p10-a6@example.com")
    await _promote_admin("p10-a6@example.com")
    owner = await _hdr(client, "p10-owner6@example.com")
    lib_id = await _create_personal(client, owner)
    await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)

    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_public"] is True
    assert body["status"] == "active"
    assert body["review_note"] is None


async def test_admin_reject_returns_personal_active(client):
    admin = await _hdr(client, "p10-a7@example.com")
    await _promote_admin("p10-a7@example.com")
    owner = await _hdr(client, "p10-owner7@example.com")
    lib_id = await _create_personal(client, owner)
    await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)

    resp = await client.post(
        f"/api/libraries/{lib_id}/reject", json={"note": "范围太宽，请收窄"}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_public"] is False
    assert body["status"] == "active"  # 退回可用的个人库，不是不可用的 rejected
    assert body["review_note"] == "范围太宽，请收窄"


async def test_approve_reject_admin_only(client):
    await _hdr(client, "p10-a8@example.com")  # 占位 admin
    owner = await _hdr(client, "p10-owner8@example.com")
    lib_id = await _create_personal(client, owner)
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=owner)
    assert resp.status_code == 403
    resp = await client.post(f"/api/libraries/{lib_id}/reject", json={}, headers=owner)
    assert resp.status_code == 403


# ---- 可见性 ----


async def test_personal_library_hidden_from_stranger(client):
    await _hdr(client, "p10-a9@example.com")  # 占位 admin
    owner = await _hdr(client, "p10-owner9@example.com")
    stranger = await _hdr(client, "p10-stranger9@example.com")
    lib_id = await _create_personal(client, owner, name="我的个人库")

    resp = await client.get("/api/libraries", headers=owner)
    assert lib_id in {x["id"] for x in resp.json()}
    resp = await client.get("/api/libraries", headers=stranger)
    assert lib_id not in {x["id"] for x in resp.json()}

    resp = await client.get(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 404
    resp = await client.get(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 200


async def test_personal_library_read_endpoints_hidden_from_stranger(client):
    """个人库的只读端点（papers/concepts/graph/notes）对非归属人 404，不经 id 泄漏内容。

    回归：修复前这些端点只做 _get_library（查存在），漏了可见性校验。转公共后陌生人可读。"""
    owner = await _hdr(client, "readvis-owner@example.com")
    stranger = await _hdr(client, "readvis-stranger@example.com")
    admin = await _hdr(client, "readvis-admin@example.com")
    await _promote_admin("readvis-admin@example.com")
    lib_id = await _create_personal(client, owner, name="只读端点个人库")

    read_paths = [
        f"/api/libraries/{lib_id}/papers",
        f"/api/libraries/{lib_id}/concepts",
        f"/api/libraries/{lib_id}/graph",
        f"/api/libraries/{lib_id}/notes",
    ]
    # 陌生人：全部 404（不泄漏）
    for path in read_paths:
        resp = await client.get(path, headers=stranger)
        assert resp.status_code == 404, (path, resp.status_code)
    # 归属人自己：可读
    resp = await client.get(f"/api/libraries/{lib_id}/papers", headers=owner)
    assert resp.status_code == 200

    # 申请转公共 + admin 批准 → 陌生人可读
    await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    assert resp.status_code == 200
    resp = await client.get(f"/api/libraries/{lib_id}/papers", headers=stranger)
    assert resp.status_code == 200


async def test_admin_sees_others_personal(client):
    admin = await _hdr(client, "p10-a10@example.com")
    await _promote_admin("p10-a10@example.com")
    owner = await _hdr(client, "p10-owner10@example.com")
    lib_id = await _create_personal(client, owner)

    resp = await client.get("/api/libraries", headers=admin)
    assert lib_id in {x["id"] for x in resp.json()}
    resp = await client.get(f"/api/libraries/{lib_id}", headers=admin)
    assert resp.status_code == 200


async def test_public_library_visible_to_all(client):
    admin = await _hdr(client, "p10-a11@example.com")
    await _promote_admin("p10-a11@example.com")
    owner = await _hdr(client, "p10-owner11@example.com")
    stranger = await _hdr(client, "p10-stranger11@example.com")
    lib_id = await _create_personal(client, owner, name="将转公共的库")
    # 转公共前陌生人看不到
    resp = await client.get("/api/libraries", headers=stranger)
    assert lib_id not in {x["id"] for x in resp.json()}
    # 申请 + 审批转公共
    await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)
    await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    # 转公共后全员可见
    resp = await client.get("/api/libraries", headers=stranger)
    assert lib_id in {x["id"] for x in resp.json()}
    resp = await client.get(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 200
    assert resp.json()["is_public"] is True


async def test_list_type_filter(client):
    admin = await _hdr(client, "p10-a12@example.com")
    await _promote_admin("p10-a12@example.com")
    owner = await _hdr(client, "p10-owner12@example.com")
    personal_id = await _create_personal(client, owner, name="留个人")
    public_id = await _create_personal(client, owner, name="转公共")
    await client.post(f"/api/libraries/{public_id}/request-public", headers=owner)
    await client.post(f"/api/libraries/{public_id}/approve", headers=admin)

    resp = await client.get("/api/libraries?type=personal", headers=owner)
    ids = {x["id"] for x in resp.json()}
    assert personal_id in ids and public_id not in ids
    resp = await client.get("/api/libraries?type=public", headers=owner)
    ids = {x["id"] for x in resp.json()}
    assert public_id in ids and personal_id not in ids


# ---- 删除权限 ----


async def test_personal_owner_can_delete(client):
    await _hdr(client, "p10-a13@example.com")  # 占位 admin
    owner = await _hdr(client, "p10-owner13@example.com")
    lib_id = await _create_personal(client, owner)
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 204, resp.text


async def test_personal_stranger_cannot_delete(client):
    admin = await _hdr(client, "p10-a14@example.com")
    await _promote_admin("p10-a14@example.com")
    owner = await _hdr(client, "p10-owner14@example.com")
    stranger = await _hdr(client, "p10-stranger14@example.com")
    lib_id = await _create_personal(client, owner)
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 403
    # 库还在
    resp = await client.get(f"/api/libraries/{lib_id}", headers=admin)
    assert resp.status_code == 200


async def test_public_library_only_admin_deletes(client):
    admin = await _hdr(client, "p10-a15@example.com")
    await _promote_admin("p10-a15@example.com")
    owner = await _hdr(client, "p10-owner15@example.com")
    lib_id = await _create_personal(client, owner)
    await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)
    await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    # 公共库创建者也不能删
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 403
    # admin 能删
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=admin)
    assert resp.status_code == 204, resp.text


# ---- 序列化：is_public / owner_name ----


async def test_overview_carries_is_public_and_owner_name(client):
    await _hdr(client, "p10-a16@example.com")  # 占位 admin
    owner_token = await register_and_login(client, email="p10-owner16@example.com")
    owner = {"Authorization": f"Bearer {owner_token}"}
    lib_id = await _create_personal(client, owner)
    resp = await client.get(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_public"] is False
    assert body["owner_name"] is not None  # 创建者展示名回填


def test_ingest_billing_owner_unit():
    """公共库 ingest 走全局 key（None）；个人库走创建者 key（submitted_by）。"""
    from app.agents.voyage.actions_wiki import _ingest_billing_owner

    owner_id = uuid.uuid4()
    public = DirectionLibrary(name="pub", is_public=True, submitted_by=owner_id)
    personal = DirectionLibrary(name="me", is_public=False, submitted_by=owner_id)
    assert _ingest_billing_owner(public) is None
    assert _ingest_billing_owner(personal) == owner_id


# ---- P10 细化：admin 直通 / 取消申请 / 转回个人 ----


async def test_admin_request_public_auto_approves(client):
    """平台 admin 发起 request-public → 直接转公共（跳过 pending 审批）。"""
    admin = await _hdr(client, "auto-admin@example.com")
    await _promote_admin("auto-admin@example.com")
    lib_id = await _create_personal(client, admin, name="admin 自建库")
    resp = await client.post(f"/api/libraries/{lib_id}/request-public", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_public"] is True and body["status"] == "active"


async def test_cancel_request_public_returns_personal(client):
    """归属人撤回 pending 申请 → 退回可用个人库。"""
    await _hdr(client, "cancel-placeholder@example.com")  # 占位 admin（首个注册用户自动 admin）
    owner = await _hdr(client, "cancel-owner@example.com")
    lib_id = await _create_personal(client, owner)
    await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)
    resp = await client.post(f"/api/libraries/{lib_id}/cancel-request-public", headers=owner)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active" and body["is_public"] is False


async def test_make_personal_admin_only(client):
    """admin 把公共库转回个人（其他人看不到）；非 admin 转个人 → 403。"""
    await _hdr(client, "mp-placeholder@example.com")  # 占位 admin（首个注册用户自动 admin）
    owner = await _hdr(client, "mp-owner@example.com")
    admin = await _hdr(client, "mp-admin@example.com")
    await _promote_admin("mp-admin@example.com")
    lib_id = await _create_personal(client, owner)
    await client.post(f"/api/libraries/{lib_id}/request-public", headers=owner)
    await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    # 非 admin 转个人 → 403
    resp = await client.post(f"/api/libraries/{lib_id}/make-personal", headers=owner)
    assert resp.status_code == 403
    # admin 转个人 → is_public false
    resp = await client.post(f"/api/libraries/{lib_id}/make-personal", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_public"] is False
    # 转回个人后，原本非归属人的陌生人看不到
    stranger = await _hdr(client, "mp-stranger@example.com")
    resp = await client.get(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 404
