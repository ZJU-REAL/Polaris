"""用户系统 U1：头像上传 / 邀请链接 / 管理员用户管理 / 功能权限与配额守卫。"""

import io
import uuid

from PIL import Image
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.llm_config import LLMUsage
from app.models.user import User
from tests.conftest import register_and_login


def _png_bytes(size=(64, 48), color=(30, 60, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


async def _me(client, token):
    resp = await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    return resp.json()


async def _create_project(client, token, name="user-sys-proj"):
    resp = await client.post(
        "/api/projects", json={"name": name}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---- 个人资料 ----


async def test_avatar_upload_and_fetch(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    me = await _me(client, token)
    assert me["has_avatar"] is False

    resp = await client.post(
        "/api/users/me/avatar",
        files={"file": ("me.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_avatar"] is True

    resp = await client.get(f"/api/users/{me['id']}/avatar", headers=headers)
    assert resp.status_code == 200
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (256, 256)  # 中心裁方 + 统一缩放

    # 非图片 → 422
    resp = await client.post(
        "/api/users/me/avatar",
        files={"file": ("x.png", b"not-an-image", "image/png")},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_update_display_name_and_usage(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.patch("/api/users/me", json={"display_name": "王小明"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "王小明"

    resp = await client.get("/api/users/me/usage", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"tokens_used": 0, "token_quota": None}


# ---- 邀请链接 ----


async def test_invite_link_flow(client):
    owner = await register_and_login(client)  # 首个用户 = admin，但这里按 owner 用
    owner_h = {"Authorization": f"Bearer {owner}"}
    pid = await _create_project(client, owner)

    resp = await client.post(
        f"/api/projects/{pid}/invites", json={"expires_days": 7, "max_uses": 2}, headers=owner_h
    )
    assert resp.status_code == 201, resp.text
    invite = resp.json()
    assert invite["used_count"] == 0

    # 新用户通过链接预览并加入
    guest = await register_and_login(client, email="guest@example.com")
    guest_h = {"Authorization": f"Bearer {guest}"}
    resp = await client.get(f"/api/invites/{invite['token']}", headers=guest_h)
    info = resp.json()
    assert info["valid"] is True and info["already_member"] is False
    assert info["project_name"] == "user-sys-proj"

    resp = await client.post(f"/api/invites/{invite['token']}/accept", headers=guest_h)
    assert resp.status_code == 200
    assert resp.json()["id"] == pid
    # 幂等：再接受一次仍 200
    resp = await client.post(f"/api/invites/{invite['token']}/accept", headers=guest_h)
    assert resp.status_code == 200

    # guest 现在能看到项目
    resp = await client.get("/api/projects", headers=guest_h)
    assert any(p["id"] == pid for p in resp.json())

    # 撤销后新用户不能再加入
    resp = await client.delete(f"/api/projects/{pid}/invites/{invite['id']}", headers=owner_h)
    assert resp.status_code == 204
    other = await register_and_login(client, email="late@example.com")
    resp = await client.post(
        f"/api/invites/{invite['token']}/accept", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 410

    # 非成员不能生成邀请
    resp = await client.post(
        f"/api/projects/{pid}/invites", json={}, headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 404


# ---- 管理员用户管理 ----


async def test_admin_user_management(client):
    admin = await register_and_login(client)  # 首个注册用户自动 admin
    admin_h = {"Authorization": f"Bearer {admin}"}
    member = await register_and_login(client, email="member@example.com")
    member_h = {"Authorization": f"Bearer {member}"}
    member_id = (await _me(client, member))["id"]
    admin_id = (await _me(client, admin))["id"]

    # 非管理员 → 403
    resp = await client.get("/api/admin/users", headers=member_h)
    assert resp.status_code == 403

    resp = await client.get("/api/admin/users", headers=admin_h)
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["email"] for r in rows} == {"alice@example.com", "member@example.com"}

    # 设置配额与功能权限
    resp = await client.patch(
        f"/api/admin/users/{member_id}",
        json={"token_quota": 1000, "features": {"forge": False, "unknown_key": True}},
        headers=admin_h,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_quota"] == 1000
    assert body["features"] == {"forge": False}  # 未知键被丢弃

    # 清除配额
    resp = await client.patch(
        f"/api/admin/users/{member_id}", json={"token_quota": -1}, headers=admin_h
    )
    assert resp.json()["token_quota"] is None

    # 不能改自己的角色
    resp = await client.patch(
        f"/api/admin/users/{admin_id}", json={"role": "member"}, headers=admin_h
    )
    assert resp.status_code == 400

    # 批量分配方向
    pid1 = await _create_project(client, admin, name="批量方向一")
    pid2 = await _create_project(client, admin, name="批量方向二")
    resp = await client.post(
        "/api/admin/users/batch-assign",
        json={"user_ids": [member_id], "project_ids": [pid1, pid2], "role": "member"},
        headers=admin_h,
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 2
    # 重复分配跳过
    resp = await client.post(
        "/api/admin/users/batch-assign",
        json={"user_ids": [member_id], "project_ids": [pid1, pid2]},
        headers=admin_h,
    )
    assert resp.json()["added"] == 0
    resp = await client.get("/api/projects", headers=member_h)
    assert {p["name"] for p in resp.json()} >= {"批量方向一", "批量方向二"}

    # 管理员全部方向列表
    resp = await client.get("/api/admin/projects", headers=admin_h)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ---- 功能权限与配额守卫 ----


async def test_feature_and_quota_guard(client):
    admin = await register_and_login(client)
    admin_h = {"Authorization": f"Bearer {admin}"}
    member = await register_and_login(client, email="worker@example.com")
    member_h = {"Authorization": f"Bearer {member}"}
    member_id = (await _me(client, member))["id"]
    pid = await _create_project(client, admin, name="守卫方向")
    await client.post(
        "/api/admin/users/batch-assign",
        json={"user_ids": [member_id], "project_ids": [pid]},
        headers=admin_h,
    )

    forge_body = {"knobs": {}}

    # 禁用 forge 功能 → 403 FEATURE_DISABLED
    await client.patch(
        f"/api/admin/users/{member_id}", json={"features": {"forge": False}}, headers=admin_h
    )
    resp = await client.post(f"/api/projects/{pid}/forge", json=forge_body, headers=member_h)
    assert resp.status_code == 403 and resp.json()["detail"] == "FEATURE_DISABLED"

    # 恢复功能、设置配额并写入超额用量 → 403 TOKEN_QUOTA_EXCEEDED
    await client.patch(
        f"/api/admin/users/{member_id}",
        json={"features": {"forge": True}, "token_quota": 100},
        headers=admin_h,
    )
    async with get_sessionmaker()() as session:
        uid = (
            await session.execute(select(User.id).where(User.email == "worker@example.com"))
        ).scalar_one()
        session.add(
            LLMUsage(
                user_id=uid,
                project_id=uuid.UUID(pid),
                stage="forge",
                model="fake",
                prompt_tokens=80,
                completion_tokens=40,
            )
        )
        await session.commit()
    resp = await client.post(f"/api/projects/{pid}/forge", json=forge_body, headers=member_h)
    assert resp.status_code == 403 and resp.json()["detail"] == "TOKEN_QUOTA_EXCEEDED"


async def test_llm_access_levels(client):
    admin = await register_and_login(client)
    admin_h = {"Authorization": f"Bearer {admin}"}
    member = await register_and_login(client, email="limited@example.com")
    member_h = {"Authorization": f"Bearer {member}"}
    member_id = (await _me(client, member))["id"]
    pid = await _create_project(client, admin, name="权限方向")
    await client.post(
        "/api/admin/users/batch-assign",
        json={"user_ids": [member_id], "project_ids": [pid]},
        headers=admin_h,
    )

    # chat_only：AI 任务被拒，文献对话放行
    resp = await client.patch(
        f"/api/admin/users/{member_id}", json={"llm_access": "chat_only"}, headers=admin_h
    )
    assert resp.status_code == 200 and resp.json()["llm_access"] == "chat_only"
    resp = await client.post(f"/api/projects/{pid}/forge", json={"knobs": {}}, headers=member_h)
    assert resp.status_code == 403 and resp.json()["detail"] == "LLM_ACCESS_CHAT_ONLY"
    async with client.stream(
        "POST",
        f"/api/projects/{pid}/chat",
        json={"question": "这些方法的共同局限是什么？"},
        headers=member_h,
    ) as resp:
        assert resp.status_code == 200  # 文献对话不受 chat_only 限制

    # blocked：连对话也被锁
    await client.patch(
        f"/api/admin/users/{member_id}", json={"llm_access": "blocked"}, headers=admin_h
    )
    resp = await client.post(
        f"/api/projects/{pid}/chat",
        json={"question": "hi"},
        headers=member_h,
    )
    assert resp.status_code == 403 and resp.json()["detail"] == "LLM_ACCESS_BLOCKED"
    resp = await client.post(f"/api/projects/{pid}/forge", json={"knobs": {}}, headers=member_h)
    assert resp.status_code == 403 and resp.json()["detail"] == "LLM_ACCESS_BLOCKED"

    # 非法取值被 422 拒绝
    resp = await client.patch(
        f"/api/admin/users/{member_id}", json={"llm_access": "sometimes"}, headers=admin_h
    )
    assert resp.status_code == 422
