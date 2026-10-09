"""幽灵 wire 字段退役核证（#734）。

这些字段曾因「golden transcript 逐字节比对，纯移除 PR 不许动 wire 形状」而以
恒值苟活；本次随 golden 政策落地（docs/golden-policy.md）做了一次**有意重录**，
从响应面上彻底移除。这里钉住「确实没了」，防止哪个序列化路径把它们带回来。
"""

from tests.conftest import register_and_login


async def test_user_responses_have_no_ghost_fields(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/users/me", headers=headers)).json()
    # 恒 False 的自管轨残留（#621 删列后只剩 wire 兼容）
    assert "llm_self_managed" not in me
    # 单人产品没有「超管」概念：fastapi-users 基类字段不再随响应下发
    assert "is_superuser" not in me


async def test_library_responses_have_no_ghost_fields(client):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    created = await client.post(
        "/api/libraries",
        json={"name": "Ghost Library", "statement": "ghost-field retirement probe"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    detail = created.json()
    # 审批流残留的恒值字段（#619 删列后只剩 wire 兼容）
    assert "status" not in detail
    assert "review_note" not in detail

    listed = (await client.get("/api/libraries", headers=headers)).json()
    mine = next(row for row in listed if row["id"] == detail["id"])
    assert "status" not in mine
    assert "review_note" not in mine
