"""用户系统 U1：头像上传 / 功能权限与配额守卫。

管理员用户管理与批量分配 API 已随去实验室化移除（#603）；守卫用的
governance 字段（features/token_quota/llm_access）仍在，测试直接写库摆状态。
"""

import io
import uuid

from PIL import Image
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.llm_config import LLMUsage
from app.models.project import ProjectMember
from app.models.user import User
from tests.conftest import register_and_login, set_user_fields


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


async def _add_member(project_id: str, email: str) -> None:
    """直接写库把用户加进课题（管理端批量分配 API 已随 #603 移除）。"""
    async with get_sessionmaker()() as session:
        uid = (await session.execute(select(User.id).where(User.email == email))).scalar_one()
        session.add(
            ProjectMember(project_id=uuid.UUID(project_id), user_id=uid, role="member")
        )
        await session.commit()


# ---- 功能权限与配额守卫 ----


async def test_feature_and_quota_guard(client):
    admin = await register_and_login(client)  # 首个注册用户=admin
    member = await register_and_login(client, email="worker@example.com")
    member_h = {"Authorization": f"Bearer {member}"}
    pid = await _create_project(client, admin, name="守卫方向")
    await _add_member(pid, "worker@example.com")

    forge_body = {"knobs": {}}

    # 禁用 forge 功能 → 403 FEATURE_DISABLED
    await set_user_fields("worker@example.com", features={"forge": False})
    resp = await client.post(f"/api/projects/{pid}/forge", json=forge_body, headers=member_h)
    assert resp.status_code == 403 and resp.json()["detail"] == "FEATURE_DISABLED"

    # 恢复功能、设置配额并写入超额用量 → 403 TOKEN_QUOTA_EXCEEDED
    await set_user_fields("worker@example.com", features={"forge": True}, token_quota=100)
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
    admin = await register_and_login(client)  # 首个注册用户=admin
    member = await register_and_login(client, email="limited@example.com")
    member_h = {"Authorization": f"Bearer {member}"}
    pid = await _create_project(client, admin, name="权限方向")
    await _add_member(pid, "limited@example.com")

    # chat_only：AI 任务被拒，文献对话放行
    await set_user_fields("limited@example.com", llm_access="chat_only")
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
    await set_user_fields("limited@example.com", llm_access="blocked")
    resp = await client.post(
        f"/api/projects/{pid}/chat",
        json={"question": "hi"},
        headers=member_h,
    )
    assert resp.status_code == 403 and resp.json()["detail"] == "LLM_ACCESS_BLOCKED"
    resp = await client.post(f"/api/projects/{pid}/forge", json={"knobs": {}}, headers=member_h)
    assert resp.status_code == 403 and resp.json()["detail"] == "LLM_ACCESS_BLOCKED"
