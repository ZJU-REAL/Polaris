"""用户系统 U1：头像上传与个人资料。

功能权限/配额/llm_access 守卫已随治理字段移除（#614），对应用例一并删除。
"""

import io

from PIL import Image

from tests.conftest import register_and_login


def _png_bytes(size=(64, 48), color=(30, 60, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


async def _me(client, token):
    resp = await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    return resp.json()


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
    assert resp.json() == {"tokens_used": 0}
