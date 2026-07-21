"""用户反馈：提交/截图/我的；管理员 triage / LLM 草稿 / 建 issue（GitHub 调用 mock）。"""

import io

from PIL import Image

from app.core import github
from tests.conftest import register_and_login


def _png(size=(80, 60), color=(40, 90, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


async def _member(client, admin_first=True):
    """确保首个用户占掉 admin，再返回一个普通成员 token。"""
    if admin_first:
        await register_and_login(client, email="admin@example.com")
    return await register_and_login(client, email="member@example.com")


async def test_submit_derives_module_and_lists_mine(client):
    token = await register_and_login(client)  # 首个用户=admin，但提交对任何登录用户开放
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/feedback",
        json={
            "type": "bug",
            "severity": "high",
            "title": "保存按钮无响应",
            "body": "1. 打开 /forge\n2. 点保存\n3. 无反应",
            "route": "/forge",
            "context": {"version": "0.3.1", "viewport": "1440x900", "env": "dev"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    fb = resp.json()
    assert fb["module"] == "forge"  # 由 route 推导
    assert fb["status"] == "new"
    assert fb["context"]["version"] == "0.3.1"

    mine = await client.get("/api/feedback/mine", headers=headers)
    assert mine.status_code == 200
    assert any(f["id"] == fb["id"] for f in mine.json())


async def test_image_upload_fetch_and_owner_guard(client):
    admin = await register_and_login(client, email="admin@example.com")
    member = await register_and_login(client, email="member@example.com")
    mheaders = {"Authorization": f"Bearer {member}"}
    fb = (
        await client.post(
            "/api/feedback", json={"title": "截图 bug", "route": "/wiki"}, headers=mheaders
        )
    ).json()

    up = await client.post(
        f"/api/feedback/{fb['id']}/images",
        files={"file": ("shot.png", _png(), "image/png")},
        headers=mheaders,
    )
    assert up.status_code == 200, up.text
    assert up.json()["seq"] == 0

    img = await client.get(f"/api/feedback/{fb['id']}/images/0", headers=mheaders)
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"

    # admin 也能看
    aheaders = {"Authorization": f"Bearer {admin}"}
    assert (
        await client.get(f"/api/feedback/{fb['id']}/images/0", headers=aheaders)
    ).status_code == 200

    # 第三个用户（非 owner 非 admin）看不到
    other = await register_and_login(client, email="other@example.com")
    oheaders = {"Authorization": f"Bearer {other}"}
    assert (
        await client.get(f"/api/feedback/{fb['id']}/images/0", headers=oheaders)
    ).status_code == 403


async def test_admin_triage_and_update(client):
    admin = await register_and_login(client, email="admin@example.com")
    member = await register_and_login(client, email="member@example.com")
    fb = (
        await client.post(
            "/api/feedback",
            json={"title": "体验问题", "type": "ui"},
            headers={"Authorization": f"Bearer {member}"},
        )
    ).json()

    aheaders = {"Authorization": f"Bearer {admin}"}
    lst = await client.get("/api/admin/feedback", headers=aheaders)
    assert lst.status_code == 200
    assert any(f["id"] == fb["id"] for f in lst.json())
    assert lst.json()[0]["author"]["display_name"]  # 带提交人

    patched = await client.patch(
        f"/api/admin/feedback/{fb['id']}",
        json={"status": "triaged", "severity": "low", "admin_note": "已确认"},
        headers=aheaders,
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "triaged"
    assert patched.json()["admin_note"] == "已确认"


async def test_generate_draft_follows_template(client):
    admin = await register_and_login(client, email="admin@example.com")
    fb = (
        await client.post(
            "/api/feedback",
            json={"title": "上传 500", "type": "bug", "body": "点上传就报 500", "route": "/wiki"},
            headers={"Authorization": f"Bearer {admin}"},
        )
    ).json()
    resp = await client.post(
        f"/api/admin/feedback/{fb['id']}/draft", headers={"Authorization": f"Bearer {admin}"}
    )
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    # fake provider 下走规则回退草稿：标题带 bug: 前缀、正文含模板章节
    assert draft["title"].startswith("bug: ")
    assert "###" in draft["body"]
    assert "bug" in draft["labels"]


async def test_create_issue_mocked_then_conflict(client, monkeypatch):
    admin = await register_and_login(client, email="admin@example.com")
    aheaders = {"Authorization": f"Bearer {admin}"}
    fb = (
        await client.post(
            "/api/feedback", json={"title": "建 issue 测试", "type": "feature"}, headers=aheaders
        )
    ).json()

    async def fake_create_issue(*, title, body, labels=None):
        return 123, "https://github.com/ZJU-REAL/Polaris/issues/123"

    monkeypatch.setattr(github, "create_issue", fake_create_issue)

    draft = {"title": "feat: 建 issue 测试", "body": "### Problem\n...", "labels": ["enhancement"]}
    resp = await client.post(f"/api/admin/feedback/{fb['id']}/issue", json=draft, headers=aheaders)
    assert resp.status_code == 200, resp.text
    assert resp.json()["number"] == 123

    # 回填并置 in_progress
    got = [
        f
        for f in (await client.get("/api/admin/feedback", headers=aheaders)).json()
        if f["id"] == fb["id"]
    ][0]
    assert got["github_issue_number"] == 123
    assert got["status"] == "in_progress"

    # 再建 → 409
    dup = await client.post(f"/api/admin/feedback/{fb['id']}/issue", json=draft, headers=aheaders)
    assert dup.status_code == 409


async def test_create_issue_without_token_400(client):
    # 测试环境未配 github_token → 建 issue 返回 400 GITHUB_NOT_CONFIGURED（不联网）
    admin = await register_and_login(client, email="admin@example.com")
    aheaders = {"Authorization": f"Bearer {admin}"}
    fb = (
        await client.post(
            "/api/feedback", json={"title": "无 token", "type": "task"}, headers=aheaders
        )
    ).json()
    draft = {"title": "task: 无 token", "body": "### Description\n...", "labels": ["task"]}
    resp = await client.post(f"/api/admin/feedback/{fb['id']}/issue", json=draft, headers=aheaders)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "GITHUB_NOT_CONFIGURED"
    assert (await client.get("/api/admin/feedback/github-status", headers=aheaders)).json()[
        "enabled"
    ] is False


async def test_issue_close_syncs_status_to_resolved(client, monkeypatch):
    # issue 关闭后，列表接口应把 in_progress 的反馈同步成 resolved（issue #117）
    admin = await register_and_login(client, email="admin@example.com")
    aheaders = {"Authorization": f"Bearer {admin}"}
    fb = (
        await client.post(
            "/api/feedback", json={"title": "同步测试", "type": "bug"}, headers=aheaders
        )
    ).json()

    async def fake_create_issue(*, title, body, labels=None):
        return 456, "https://github.com/ZJU-REAL/Polaris/issues/456"

    monkeypatch.setattr(github, "create_issue", fake_create_issue)
    draft = {"title": "bug: 同步测试", "body": "### Summary\n...", "labels": ["bug"]}
    assert (
        await client.post(f"/api/admin/feedback/{fb['id']}/issue", json=draft, headers=aheaders)
    ).status_code == 200

    # issue 仍 open：状态保持 in_progress
    async def states_open(numbers):
        return {n: "open" for n in numbers}

    monkeypatch.setattr(github, "fetch_issue_states", states_open)
    mine = (await client.get("/api/feedback/mine", headers=aheaders)).json()
    assert [f for f in mine if f["id"] == fb["id"]][0]["status"] == "in_progress"

    # issue 关闭：TTL 内不重查，绕过节流后应变 resolved
    from app.services import feedback as svc

    svc._last_synced.clear()

    async def states_closed(numbers):
        return {n: "closed" for n in numbers}

    monkeypatch.setattr(github, "fetch_issue_states", states_closed)
    mine = (await client.get("/api/feedback/mine", headers=aheaders)).json()
    assert [f for f in mine if f["id"] == fb["id"]][0]["status"] == "resolved"

    # resolved 属终态：后续列表不再触发查询
    async def states_boom(numbers):
        raise AssertionError("should not query terminal feedback")

    monkeypatch.setattr(github, "fetch_issue_states", states_boom)
    resp = await client.get("/api/feedback/mine", headers=aheaders)
    assert resp.status_code == 200


async def test_non_admin_cannot_triage(client):
    member = await _member(client)
    mheaders = {"Authorization": f"Bearer {member}"}
    assert (await client.get("/api/admin/feedback", headers=mheaders)).status_code == 403
    fb = (await client.post("/api/feedback", json={"title": "x"}, headers=mheaders)).json()
    assert (
        await client.post(f"/api/admin/feedback/{fb['id']}/draft", headers=mheaders)
    ).status_code == 403
