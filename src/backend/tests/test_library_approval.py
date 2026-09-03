"""文献库归属与可见性 —— 建库即个人库（转公共审批流已随实验室定位移除，
status/review_note 残留列随 #619 删除）。

- 任意登录用户建库 → 即刻可用的**个人库**（is_public=false）；
- 个人库仅创建者可见/可管理，token 记创建者账（admin 旁路已随 #614 移除）；
- 存量公共库（is_public=true）仍全员可见；
- 删除：一律创建者本人。
"""

import uuid

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _make_public(lib_id: str) -> None:
    """直接把库置为公共（造数据快捷方式；正路是创建者 PATCH is_public，见下方专测）。"""
    async with get_sessionmaker()() as session:
        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        lib.is_public = True
        await session.commit()


async def _create_personal(client, headers, name="用户建的库"):
    resp = await client.post(
        "/api/libraries",
        json={"name": name, "statement": "一句话方向陈述", "anchors": ["2401.00001"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_public"] is False
    return body["id"]


# ---- 建库即 active 个人库 ----


async def test_user_creates_personal_active_library(client):
    await _hdr(client, "p10-a1@example.com")  # 占位 admin
    user = await _hdr(client, "p10-u1@example.com")
    lib_id = await _create_personal(client, user)
    async with get_sessionmaker()() as session:
        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        assert lib.is_public is False
        assert lib.submitted_by is not None
        assert lib.definition["anchor_papers"] == ["2401.00001"]


async def test_personal_library_can_ingest_without_approval(client, queue_stub):
    """P10：个人库即刻 active，无需审批即可触发抓取。"""
    admin = await _hdr(client, "p10-a2@example.com")
    lib_id = await _create_personal(client, admin)
    resp = await client.post(
        f"/api/libraries/{lib_id}/ingest/run", json={"mode": "bootstrap"}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    assert queue_stub.jobs, "个人 active 库触发应入队"


# ---- 申请转公共 + 审批 ----


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
    """个人库的只读端点（papers/concepts/graph/notes/建库同步状态）对非归属人 404，不泄漏内容。

    回归：修复前这些端点只做 _get_library（查存在），漏了可见性校验。转公共后陌生人可读。"""
    owner = await _hdr(client, "readvis-owner@example.com")
    stranger = await _hdr(client, "readvis-stranger@example.com")
    await _hdr(client, "readvis-admin@example.com")
    lib_id = await _create_personal(client, owner, name="只读端点个人库")

    read_paths = [
        f"/api/libraries/{lib_id}/papers",
        f"/api/libraries/{lib_id}/concepts",
        f"/api/libraries/{lib_id}/graph",
        f"/api/libraries/{lib_id}/notes",
        f"/api/libraries/{lib_id}/ingest/state",
    ]
    # 陌生人：全部 404（不泄漏）
    for path in read_paths:
        resp = await client.get(path, headers=stranger)
        assert resp.status_code == 404, (path, resp.status_code)
    # 归属人自己：可读
    resp = await client.get(f"/api/libraries/{lib_id}/papers", headers=owner)
    assert resp.status_code == 200

    # 申请转公共 + admin 批准 → 陌生人可读
    await _make_public(lib_id)
    assert resp.status_code == 200
    resp = await client.get(f"/api/libraries/{lib_id}/papers", headers=stranger)
    assert resp.status_code == 200


async def test_others_personal_library_stays_hidden(client):
    """admin 全局可见旁路已随 role 移除（#614）：别人的个人库对任何用户都不可见。"""
    other = await _hdr(client, "p10-a10@example.com")
    owner = await _hdr(client, "p10-owner10@example.com")
    lib_id = await _create_personal(client, owner)

    resp = await client.get("/api/libraries", headers=other)
    assert lib_id not in {x["id"] for x in resp.json()}
    resp = await client.get(f"/api/libraries/{lib_id}", headers=other)
    assert resp.status_code == 404


async def test_public_library_visible_to_all(client):
    await _hdr(client, "p10-a11@example.com")
    owner = await _hdr(client, "p10-owner11@example.com")
    stranger = await _hdr(client, "p10-stranger11@example.com")
    lib_id = await _create_personal(client, owner, name="将转公共的库")
    # 转公共前陌生人看不到
    resp = await client.get("/api/libraries", headers=stranger)
    assert lib_id not in {x["id"] for x in resp.json()}
    # 申请 + 审批转公共
    await _make_public(lib_id)
    # 转公共后全员可见
    resp = await client.get("/api/libraries", headers=stranger)
    assert lib_id in {x["id"] for x in resp.json()}
    resp = await client.get(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 200
    assert resp.json()["is_public"] is True


async def test_digest_is_readable_by_anyone_who_can_see_the_library(client):
    """每日简报跟随**库可见性**，不跟随管理权限。

    简报是读物，不是管理动作：公共库全员可读，它的简报也该全员可读。界面上一度只有
    管理者视角（工作台）挂着「每日简报」标签，只读浏览视角没有，于是没有管理权的人
    在有简报的库里根本看不到它——公共库里几个 submitted_by 为空的，实际就成了
    只有平台管理员看得见。
    """
    import datetime as dt
    import uuid as _uuid

    from app.models.research_digest import LibraryResearchDigest

    await _hdr(client, "digest-admin@example.com")
    owner = await _hdr(client, "digest-owner@example.com")
    stranger = await _hdr(client, "digest-stranger@example.com")

    lib_id = await _create_personal(client, owner, name="有简报的库")
    await _make_public(lib_id)

    async with get_sessionmaker()() as session:
        session.add(
            LibraryResearchDigest(
                library_id=_uuid.UUID(lib_id),
                report_date=dt.date(2026, 7, 30),
                source="voyage",
                mode="incremental",
                counts={"kept": 2},
                source_diagnostics={},
                paper_insights=[],
                excluded_papers=[],
                cross_paper_signals=[],
                summary="本期两篇。",
                content="# 每日文献简报",
                rolling_trends=[],
                trend_content="",
            )
        )
        await session.commit()

    # 没有任何管理权的普通用户：列表读得到，正文也读得到
    resp = await client.get(f"/api/libraries/{lib_id}/digests", headers=stranger)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1 and rows[0]["counts"]["kept"] == 2

    resp = await client.get(f"/api/libraries/{lib_id}/digests/{rows[0]['id']}", headers=stranger)
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"].startswith("# 每日文献简报")

    # 生成仍然是管理动作：只读的人拿不到
    resp = await client.post(f"/api/libraries/{lib_id}/digests/generate", headers=stranger)
    assert resp.status_code in (403, 404), resp.text


async def test_personal_library_digest_stays_hidden_from_strangers(client):
    """个人库的简报不能因为「简报全员可读」而漏出去——它跟随库可见性，库看不见就 404。"""
    import datetime as dt
    import uuid as _uuid

    from app.models.research_digest import LibraryResearchDigest

    owner = await _hdr(client, "digest-priv-owner@example.com")
    stranger = await _hdr(client, "digest-priv-stranger@example.com")
    lib_id = await _create_personal(client, owner, name="私有库")

    async with get_sessionmaker()() as session:
        session.add(
            LibraryResearchDigest(
                library_id=_uuid.UUID(lib_id),
                report_date=dt.date(2026, 7, 30),
                source="voyage",
                mode="incremental",
                counts={},
                source_diagnostics={},
                paper_insights=[],
                excluded_papers=[],
                cross_paper_signals=[],
                summary="私有",
                content="# 私有简报",
                rolling_trends=[],
                trend_content="",
            )
        )
        await session.commit()

    assert (
        await client.get(f"/api/libraries/{lib_id}/digests", headers=stranger)
    ).status_code == 404
    assert (await client.get(f"/api/libraries/{lib_id}/digests", headers=owner)).status_code == 200


async def test_list_type_filter(client):
    await _hdr(client, "p10-a12@example.com")
    owner = await _hdr(client, "p10-owner12@example.com")
    personal_id = await _create_personal(client, owner, name="留个人")
    public_id = await _create_personal(client, owner, name="转公共")
    await _make_public(public_id)

    resp = await client.get("/api/libraries?type=personal", headers=owner)
    ids = {x["id"] for x in resp.json()}
    assert personal_id in ids and public_id not in ids
    resp = await client.get("/api/libraries?type=public", headers=owner)
    ids = {x["id"] for x in resp.json()}
    assert public_id in ids and personal_id not in ids


# ---- 共享开关：创建者直接切换 is_public（#619，审批流的替代物） ----


async def test_creator_toggles_is_public_via_settings(client):
    """审批流删掉后，「公开给所有人」就是创建者在库设置里直接拨的开关。

    - 非创建者拨不动（403）；
    - 打开 → 全员可见；关上 → 回到仅创建者可见；
    - 显式传 null 视为不改（这个开关没有「清空」语义，误清成 False 会把公共库藏起来）。
    """
    owner = await _hdr(client, "toggle-owner@example.com")
    stranger = await _hdr(client, "toggle-stranger@example.com")
    lib_id = await _create_personal(client, owner, name="开关测试库")

    resp = await client.patch(
        f"/api/libraries/{lib_id}", json={"is_public": True}, headers=stranger
    )
    assert resp.status_code == 403, resp.text

    resp = await client.patch(f"/api/libraries/{lib_id}", json={"is_public": True}, headers=owner)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_public"] is True
    assert (await client.get(f"/api/libraries/{lib_id}", headers=stranger)).status_code == 200

    resp = await client.patch(f"/api/libraries/{lib_id}", json={"is_public": False}, headers=owner)
    assert resp.json()["is_public"] is False
    resp = await client.patch(f"/api/libraries/{lib_id}", json={"is_public": None}, headers=owner)
    assert resp.json()["is_public"] is False
    assert (await client.get(f"/api/libraries/{lib_id}", headers=stranger)).status_code == 404


# ---- 删除权限 ----


async def test_personal_owner_can_delete(client):
    await _hdr(client, "p10-a13@example.com")  # 占位 admin
    owner = await _hdr(client, "p10-owner13@example.com")
    lib_id = await _create_personal(client, owner)
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 204, resp.text


async def test_personal_stranger_cannot_delete(client):
    owner = await _hdr(client, "p10-owner14@example.com")
    stranger = await _hdr(client, "p10-stranger14@example.com")
    lib_id = await _create_personal(client, owner)
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 403
    # 库还在
    resp = await client.get(f"/api/libraries/{lib_id}", headers=owner)
    assert resp.status_code == 200


async def test_public_library_creator_deletes(client):
    """删库权限收敛为创建者本人（#614）：公共库也一样，非创建者不能删。"""
    stranger = await _hdr(client, "p10-a15@example.com")
    owner = await _hdr(client, "p10-owner15@example.com")
    lib_id = await _create_personal(client, owner)
    await _make_public(lib_id)
    # 非创建者不能删（此前的 admin 直通已移除）
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=stranger)
    assert resp.status_code == 403
    # 创建者能删
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=owner)
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


